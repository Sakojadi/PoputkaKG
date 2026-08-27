import importlib

import pytest

import bot.config


def _settings(monkeypatch, **env):
    """Build a fresh Settings() with the given environment overrides."""
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    importlib.reload(bot.config)
    return bot.config.config


def test_sandbox_mode_selects_dev_base_url(monkeypatch):
    cfg = _settings(monkeypatch, XPAY_MODE="sandbox")
    assert cfg.xpay_base_url == "https://devapi.xpay.kg"


def test_production_mode_selects_live_base_url(monkeypatch):
    cfg = _settings(monkeypatch, XPAY_MODE="production")
    assert cfg.xpay_base_url == "https://api.xpay.kg"


def test_unknown_mode_is_rejected_at_startup(monkeypatch):
    with pytest.raises(Exception):  # noqa: B017 -- pydantic's ValidationError, kept generic
        _settings(monkeypatch, XPAY_MODE="sandbx")


def test_public_base_url_is_none_when_nothing_is_set(monkeypatch):
    cfg = _settings(monkeypatch, PUBLIC_BASE_URL=None, RAILWAY_PUBLIC_DOMAIN=None)
    assert cfg.resolved_public_base_url is None


def test_public_base_url_falls_back_to_railway_domain(monkeypatch):
    cfg = _settings(
        monkeypatch, PUBLIC_BASE_URL=None, RAILWAY_PUBLIC_DOMAIN="poputka.up.railway.app"
    )
    assert cfg.resolved_public_base_url == "https://poputka.up.railway.app"


def test_explicit_public_base_url_wins_and_trailing_slash_is_stripped(monkeypatch):
    cfg = _settings(
        monkeypatch,
        PUBLIC_BASE_URL="https://example.kg/",
        RAILWAY_PUBLIC_DOMAIN="ignored.up.railway.app",
    )
    assert cfg.resolved_public_base_url == "https://example.kg"


def test_xpay_api_key_setting_is_gone(monkeypatch):
    cfg = _settings(monkeypatch, XPAY_MODE="sandbox")
    assert not hasattr(cfg, "xpay_api_key")
