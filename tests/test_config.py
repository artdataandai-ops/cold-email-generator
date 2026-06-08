"""Tests for src/config.py builders.

Each test re-imports src.config under a patched environment so the module-level
env reads pick up the test values. We can't just patch attributes after import
because helpers like _proxy_block close over the module-level constants.
"""
from __future__ import annotations

import importlib
import sys
from typing import Any

import pytest


def _reload_config(monkeypatch: pytest.MonkeyPatch, **env: str) -> Any:
    """Reload src.config with the given env vars set, return the fresh module."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("src.config", None)
    return importlib.import_module("src.config")


def test_no_proxy_when_server_blank(monkeypatch):
    cfg_mod = _reload_config(monkeypatch, SCRAPEGRAPH_PROXY_SERVER="")
    cfg = cfg_mod.build_graph_config("https://example.com/")
    assert "proxy" not in cfg["loader_kwargs"]


def test_explicit_proxy_includes_credentials(monkeypatch):
    cfg_mod = _reload_config(
        monkeypatch,
        SCRAPEGRAPH_PROXY_SERVER="http://proxy.example.com:8080",
        SCRAPEGRAPH_PROXY_USERNAME="alice",
        SCRAPEGRAPH_PROXY_PASSWORD="secret",
    )
    cfg = cfg_mod.build_graph_config("https://example.com/")
    assert cfg["loader_kwargs"]["proxy"] == {
        "server": "http://proxy.example.com:8080",
        "username": "alice",
        "password": "secret",
    }


def test_broker_proxy_without_credentials(monkeypatch):
    cfg_mod = _reload_config(
        monkeypatch,
        SCRAPEGRAPH_PROXY_SERVER="broker",
        SCRAPEGRAPH_PROXY_USERNAME="",
        SCRAPEGRAPH_PROXY_PASSWORD="",
    )
    cfg = cfg_mod.build_graph_config("https://example.com/")
    assert cfg["loader_kwargs"]["proxy"] == {"server": "broker"}


def test_load_state_and_retry_defaults(monkeypatch):
    # Even with no env override the defaults must land in loader_kwargs.
    cfg_mod = _reload_config(monkeypatch)
    kw = cfg_mod.build_graph_config("https://example.com/")["loader_kwargs"]
    assert kw["load_state"] == "domcontentloaded"
    assert kw["retry_limit"] == 2
    assert kw["timeout"] == 60


def test_load_state_override(monkeypatch):
    cfg_mod = _reload_config(
        monkeypatch,
        SCRAPEGRAPH_LOAD_STATE="networkidle",
        SCRAPEGRAPH_RETRY_LIMIT="5",
        SCRAPEGRAPH_PLAYWRIGHT_TIMEOUT="90",
    )
    kw = cfg_mod.build_graph_config("https://example.com/")["loader_kwargs"]
    assert kw["load_state"] == "networkidle"
    assert kw["retry_limit"] == 5
    assert kw["timeout"] == 90


def test_stealth_config_forces_networkidle_and_headed(monkeypatch):
    cfg_mod = _reload_config(monkeypatch, SCRAPEGRAPH_LOAD_STATE="domcontentloaded")
    cfg = cfg_mod.build_stealth_config("https://example.com/")
    assert cfg["headless"] is False
    assert cfg["loader_kwargs"]["load_state"] == "networkidle"


def test_stealth_config_preserves_other_loader_kwargs(monkeypatch):
    cfg_mod = _reload_config(
        monkeypatch,
        SCRAPEGRAPH_PROXY_SERVER="http://proxy:1",
        SCRAPEGRAPH_RETRY_LIMIT="4",
    )
    cfg = cfg_mod.build_stealth_config("https://example.com/")
    assert cfg["loader_kwargs"]["proxy"] == {"server": "http://proxy:1"}
    assert cfg["loader_kwargs"]["retry_limit"] == 4
    assert cfg["loader_kwargs"]["load_state"] == "networkidle"
