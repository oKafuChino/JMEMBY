import pytest

from jmemby.config import Settings


def test_settings_rejects_missing_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1")
    with pytest.raises(ValueError, match="TOKEN"):
        Settings.from_env()


def test_settings_rejects_invalid_user(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "0")
    with pytest.raises(ValueError, match="numeric|least"):
        Settings.from_env()


def test_settings_requires_higher_resume_threshold(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("MIN_FREE_SPACE_GB", "10")
    monkeypatch.setenv("RESUME_FREE_SPACE_GB", "10")
    with pytest.raises(ValueError, match="greater"):
        Settings.from_env()
