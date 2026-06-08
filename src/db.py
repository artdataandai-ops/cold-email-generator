from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from sqlalchemy import event
from sqlmodel import Field, Session, SQLModel, create_engine

from .config import PROJECT_ROOT

logger = logging.getLogger(__name__)

DB_DIR = PROJECT_ROOT / "kb"
DEFAULT_DB_PATH = DB_DIR / "app.db"
DEFAULT_DB_URL = f"sqlite:///{DEFAULT_DB_PATH}"

_engine = None
_db_url_override: Optional[str] = None


class SenderProfile(SQLModel, table=True):
    """Sender-side knowledge base entry. One profile = one offering / campaign / business unit."""

    id: str = Field(primary_key=True)
    name: str = Field(index=True)
    description: str = ""
    kb_text: str = ""
    created_at: str
    updated_at: str


class AppSettings(SQLModel, table=True):
    """Runtime-tunable settings. Singleton row (id=1). First-time values bootstrap from .env."""

    id: int = Field(default=1, primary_key=True)
    recency_days: int
    max_news_items: int
    max_discovered_urls: int
    search_max_results: int
    disable_web_search: bool
    linkedin_post_limit: int
    default_intent: Optional[str] = Field(default=None)
    disable_linkedin: Optional[bool] = Field(default=None)
    # When true, the pipeline skips email drafting if no news/LinkedIn context was found.
    # Optional + nullable for back-compat with rows written before this column existed.
    require_context_for_email: Optional[bool] = Field(default=None)
    updated_at: str


def set_db_url(url: Optional[str]) -> None:
    """Override the DB URL. Pass None to revert to the default. Used by tests."""
    global _engine, _db_url_override
    _db_url_override = url
    _engine = None


def get_engine():
    global _engine
    if _engine is not None:
        return _engine

    url = _db_url_override or DEFAULT_DB_URL
    if url == DEFAULT_DB_URL:
        DB_DIR.mkdir(parents=True, exist_ok=True)

    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    SQLModel.metadata.create_all(engine)
    _ensure_schema_up_to_date(engine)
    _engine = engine

    if not _db_url_override:
        _migrate_from_legacy_json()

    return engine


def _ensure_schema_up_to_date(engine) -> None:
    """Add columns introduced after the initial table creation.

    SQLModel.metadata.create_all only creates missing tables — it does not add columns
    to existing tables. We do a one-shot ALTER for each column added in a later release.
    """
    with engine.connect() as conn:
        existing = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(appsettings)").fetchall()]
        if existing and "default_intent" not in existing:
            conn.exec_driver_sql("ALTER TABLE appsettings ADD COLUMN default_intent TEXT")
            conn.commit()
            logger.info("Migration: added column appsettings.default_intent")
        if existing and "disable_linkedin" not in existing:
            conn.exec_driver_sql("ALTER TABLE appsettings ADD COLUMN disable_linkedin INTEGER")
            conn.commit()
            logger.info("Migration: added column appsettings.disable_linkedin")
        if existing and "require_context_for_email" not in existing:
            conn.exec_driver_sql("ALTER TABLE appsettings ADD COLUMN require_context_for_email INTEGER")
            conn.commit()
            logger.info("Migration: added column appsettings.require_context_for_email")


def get_session() -> Session:
    return Session(get_engine())


def _migrate_from_legacy_json() -> None:
    """One-time import of the old kb/profiles.json into the SQLite DB."""
    legacy = DB_DIR / "profiles.json"
    if not legacy.exists():
        return
    try:
        rows = json.loads(legacy.read_text(encoding="utf-8")) or []
    except json.JSONDecodeError:
        logger.warning("Legacy profiles.json is not valid JSON; skipping migration.")
        return
    if not rows:
        legacy.rename(legacy.with_suffix(".json.migrated"))
        return

    imported = 0
    with Session(_engine) as session:
        for raw in rows:
            pid = raw.get("id")
            if not pid or session.get(SenderProfile, pid) is not None:
                continue
            session.add(SenderProfile(
                id=pid,
                name=raw.get("name", ""),
                description=raw.get("description", ""),
                kb_text=raw.get("kb_text", ""),
                created_at=raw.get("created_at", ""),
                updated_at=raw.get("updated_at", ""),
            ))
            imported += 1
        session.commit()

    legacy.rename(legacy.with_suffix(".json.migrated"))
    logger.info("Migrated %d sender profile(s) from profiles.json into SQLite.", imported)
