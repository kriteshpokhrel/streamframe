import os
from pathlib import Path

import pytest

from streamframe.publish.settings import (
    ConfigError,
    Settings,
    load_env_file,
    parse_env,
)


def test_parse_env_handles_comments_quotes_and_export():
    text = """
# comment
export PLAIN=value
QUOTED="line\\nbreak"
SINGLE='a # not a comment'
SPACED=keep me
INLINE=value # trailing comment
"""
    values = parse_env(text)
    assert values["PLAIN"] == "value"
    assert values["QUOTED"] == "line\nbreak"
    assert values["SINGLE"] == "a # not a comment"
    assert values["SPACED"] == "keep me"
    assert values["INLINE"] == "value"


def test_env_file_does_not_override_the_shell(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("SHARED=from-file\nONLY_FILE=yes\n", encoding="utf-8")
    environ = {"SHARED": "from-shell"}
    load_env_file(env_file, environ)
    assert environ["SHARED"] == "from-shell"
    assert environ["ONLY_FILE"] == "yes"


def test_settings_select_defaults_and_reject_unknown_platforms(tmp_path):
    settings = Settings.load(data_dir=tmp_path, environ={})
    assert settings.platforms == ("youtube", "instagram", "tiktok")
    assert settings.state_dir == tmp_path / "publish"
    assert settings.youtube.privacy == "public"

    with pytest.raises(ConfigError):
        Settings.load(data_dir=tmp_path, environ={"STREAMFRAME_PUBLISH_PLATFORMS": "youtube,myspace"})


def test_problems_explain_every_missing_piece(tmp_path):
    settings = Settings.load(data_dir=tmp_path, environ={})
    problems = dict(settings.problems())
    assert set(problems) == {"youtube", "instagram", "tiktok"}
    assert "STREAMFRAME_YOUTUBE_CLIENT_ID" in problems["youtube"]
    assert "public media URL" in problems["instagram"]
    assert "STREAMFRAME_TIKTOK_CLIENT_KEY" in problems["tiktok"]


def test_media_host_configuration_is_validated(tmp_path):
    settings = Settings.load(data_dir=tmp_path, environ={
        "STREAMFRAME_MEDIA_HOST": "r2",
        "STREAMFRAME_MEDIA_PUBLIC_BASE_URL": "https://clips.example.com",
        "STREAMFRAME_R2_BUCKET": "b",
        "STREAMFRAME_R2_ENDPOINT": "https://acct.r2.cloudflarestorage.com",
        "STREAMFRAME_R2_ACCESS_KEY_ID": "id",
        "STREAMFRAME_R2_SECRET_ACCESS_KEY": "secret",
    })
    assert settings.media.configured

    with pytest.raises(ConfigError):
        Settings.load(data_dir=tmp_path, environ={"STREAMFRAME_MEDIA_HOST": "ftp"})


def test_env_file_is_discovered_from_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("STREAMFRAME_PUBLISH_ENV", raising=False)
    (tmp_path / ".env").write_text("STREAMFRAME_TIKTOK_CLIENT_KEY=abc\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    settings = Settings.load(data_dir=tmp_path / "data", environ={})
    assert settings.env_file == tmp_path / ".env"
    assert settings.tiktok.client_key == "abc"
