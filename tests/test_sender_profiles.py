from __future__ import annotations

import pytest


@pytest.fixture
def sp(tmp_path):
    """Point the DB engine at a fresh tmp SQLite file for each test."""
    from src import db, sender_profiles
    db.set_db_url(f"sqlite:///{tmp_path / 'test.db'}")
    yield sender_profiles
    db.set_db_url(None)


def test_list_empty_initially(sp):
    assert sp.list_profiles() == []


def test_create_then_list(sp):
    p = sp.create_profile(name="Acme AI", description="AI for enterprise", kb_text="...")
    profiles = sp.list_profiles()
    assert len(profiles) == 1
    assert profiles[0].name == "Acme AI"
    assert profiles[0].id == p.id


def test_get_profile(sp):
    p = sp.create_profile(name="X")
    found = sp.get_profile(p.id)
    assert found is not None
    assert found.id == p.id
    assert sp.get_profile("nonexistent") is None


def test_update_profile(sp):
    p = sp.create_profile(name="Old")
    updated = sp.update_profile(p.id, name="New", kb_text="updated body")
    assert updated is not None
    assert updated.name == "New"
    assert updated.kb_text == "updated body"
    assert updated.updated_at >= updated.created_at


def test_update_missing_returns_none(sp):
    assert sp.update_profile("nope", name="X") is None


def test_delete_profile(sp):
    p = sp.create_profile(name="ToDelete")
    assert sp.delete_profile(p.id) is True
    assert sp.list_profiles() == []
    assert sp.delete_profile(p.id) is False


def test_persistence_round_trip(tmp_path):
    """Survive an engine reset (simulates app restart) against the same DB file."""
    from src import db, sender_profiles
    db_url = f"sqlite:///{tmp_path / 'persist.db'}"
    db.set_db_url(db_url)
    try:
        sender_profiles.create_profile(name="Persistent")
        # simulate restart: drop the engine, reconnect to the same file
        db.set_db_url(None)
        db.set_db_url(db_url)
        found = sender_profiles.list_profiles()
        assert len(found) == 1
        assert found[0].name == "Persistent"
    finally:
        db.set_db_url(None)


def test_list_sorted_by_name(sp):
    sp.create_profile(name="Zebra")
    sp.create_profile(name="Alpha")
    sp.create_profile(name="Mango")
    names = [p.name for p in sp.list_profiles()]
    assert names == ["Alpha", "Mango", "Zebra"]
