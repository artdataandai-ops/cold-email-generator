from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import select

from .db import SenderProfile, get_session


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_profiles() -> list[SenderProfile]:
    with get_session() as session:
        return list(session.exec(select(SenderProfile).order_by(SenderProfile.name)).all())


def get_profile(profile_id: str) -> Optional[SenderProfile]:
    with get_session() as session:
        return session.get(SenderProfile, profile_id)


def create_profile(*, name: str, description: str = "", kb_text: str = "") -> SenderProfile:
    now = _utcnow_iso()
    profile = SenderProfile(
        id=str(uuid.uuid4()),
        name=name.strip(),
        description=description.strip(),
        kb_text=kb_text,
        created_at=now,
        updated_at=now,
    )
    with get_session() as session:
        session.add(profile)
        session.commit()
        session.refresh(profile)
    return profile


def update_profile(profile_id: str, *, name: Optional[str] = None, description: Optional[str] = None,
                   kb_text: Optional[str] = None) -> Optional[SenderProfile]:
    with get_session() as session:
        profile = session.get(SenderProfile, profile_id)
        if profile is None:
            return None
        if name is not None:
            profile.name = name.strip()
        if description is not None:
            profile.description = description.strip()
        if kb_text is not None:
            profile.kb_text = kb_text
        profile.updated_at = _utcnow_iso()
        session.add(profile)
        session.commit()
        session.refresh(profile)
        return profile


def delete_profile(profile_id: str) -> bool:
    with get_session() as session:
        profile = session.get(SenderProfile, profile_id)
        if profile is None:
            return False
        session.delete(profile)
        session.commit()
    return True
