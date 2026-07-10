"""Tests for the deploy/version stamp (config/version.py) shown in the footer.

resolve() must never raise and must honor its source precedence:
env var > VERSION file > git > "dev". The footer relies on it running on
every request via the context processor, so a throw here would 500 the whole
site -- hence the "never raises" guarantee is load-bearing, not cosmetic.
"""

import config.version as version
from config.context_processors import app_version


def test_env_var_wins(monkeypatch, tmp_path):
    # Even with a VERSION file present, the explicit env var takes precedence.
    (tmp_path / "VERSION").write_text("filesha 2026-01-01T00:00:00+00:00\n")
    monkeypatch.setattr(version, "BASE_DIR", tmp_path)
    monkeypatch.setenv("APP_VERSION", "release-9")
    monkeypatch.setenv("APP_DEPLOYED_AT", "2026-07-10T12:00:00+00:00")
    assert version.resolve() == {
        "version": "release-9",
        "deployed_at": "2026-07-10T12:00:00+00:00",
    }


def test_version_file_parsed(monkeypatch, tmp_path):
    monkeypatch.delenv("APP_VERSION", raising=False)
    (tmp_path / "VERSION").write_text("abc1234 2026-07-10T04:47:00+00:00\n")
    monkeypatch.setattr(version, "BASE_DIR", tmp_path)
    assert version.resolve() == {
        "version": "abc1234",
        "deployed_at": "2026-07-10T04:47:00+00:00",
    }


def test_version_file_without_timestamp(monkeypatch, tmp_path):
    monkeypatch.delenv("APP_VERSION", raising=False)
    (tmp_path / "VERSION").write_text("abc1234\n")
    monkeypatch.setattr(version, "BASE_DIR", tmp_path)
    assert version.resolve() == {"version": "abc1234", "deployed_at": None}


def test_falls_back_to_dev(monkeypatch, tmp_path):
    # No env var, no VERSION file, and no git repo at the (tmp) BASE_DIR.
    monkeypatch.delenv("APP_VERSION", raising=False)
    monkeypatch.setattr(version, "BASE_DIR", tmp_path)
    monkeypatch.setattr(version, "_from_git", lambda: (None, None))
    assert version.resolve() == {"version": "dev", "deployed_at": None}


def test_context_processor_shape():
    result = app_version(request=None)
    assert set(result) == {"app_version"}
    assert set(result["app_version"]) == {"version", "deployed_at"}
