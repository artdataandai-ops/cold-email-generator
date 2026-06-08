from __future__ import annotations

import pytest


@pytest.fixture
def settings_module(tmp_path):
    """Point the DB engine at a fresh tmp SQLite file for each test."""
    from src import db, settings
    db.set_db_url(f"sqlite:///{tmp_path / 'settings_test.db'}")
    yield settings
    db.set_db_url(None)


def test_first_call_bootstraps_from_env(settings_module):
    s = settings_module.get_settings()
    # Defaults from .env / config.py
    assert s.id == 1
    assert isinstance(s.recency_days, int)
    assert isinstance(s.max_news_items, int)
    assert s.recency_days >= 1


def test_get_is_idempotent(settings_module):
    s1 = settings_module.get_settings()
    s2 = settings_module.get_settings()
    assert s1.id == s2.id == 1
    assert s1.updated_at == s2.updated_at  # not re-bootstrapped on second call


def test_update_persists(settings_module):
    settings_module.get_settings()  # bootstrap
    updated = settings_module.update_settings(recency_days=15, disable_web_search=True)
    assert updated.recency_days == 15
    assert updated.disable_web_search is True
    again = settings_module.get_settings()
    assert again.recency_days == 15
    assert again.disable_web_search is True


def test_partial_update_leaves_others_alone(settings_module):
    initial = settings_module.get_settings()
    settings_module.update_settings(max_news_items=3)
    after = settings_module.get_settings()
    assert after.max_news_items == 3
    assert after.recency_days == initial.recency_days  # untouched


def test_reset_re_bootstraps(settings_module):
    settings_module.update_settings(recency_days=999)
    assert settings_module.get_settings().recency_days == 999
    fresh = settings_module.reset_settings()
    # After reset, we re-bootstrap from env defaults — recency_days should NOT be 999
    assert fresh.recency_days != 999


def test_default_intent_bootstraps_to_fallback(settings_module):
    s = settings_module.get_settings()
    assert s.default_intent
    assert "introduce" in s.default_intent.lower() or "discovery" in s.default_intent.lower()


def test_default_intent_round_trip(settings_module):
    custom = "Invite the prospect to our private beta of Acme Studio next month."
    updated = settings_module.update_settings(default_intent=custom)
    assert updated.default_intent == custom
    again = settings_module.get_settings()
    assert again.default_intent == custom


def test_default_intent_empty_string_falls_back_to_fallback(settings_module):
    # User sends "" — we coerce to the fallback rather than persisting an empty intent.
    updated = settings_module.update_settings(default_intent="   ")
    assert updated.default_intent
    assert updated.default_intent.strip() != ""


def test_disable_linkedin_bootstraps_to_env_default(settings_module):
    # Env default is "false" (LinkedIn enabled).
    s = settings_module.get_settings()
    assert s.disable_linkedin is False


def test_disable_linkedin_round_trip(settings_module):
    settings_module.get_settings()  # bootstrap
    updated = settings_module.update_settings(disable_linkedin=True)
    assert updated.disable_linkedin is True
    again = settings_module.get_settings()
    assert again.disable_linkedin is True
    # Toggle back
    settings_module.update_settings(disable_linkedin=False)
    assert settings_module.get_settings().disable_linkedin is False
