import json

from streamframe.cli import build_parser
from streamframe.publish.cli import run as run_publish


def parse(*argv):
    return build_parser().parse_args(["publish", *argv])


def test_publish_is_available_without_starting_the_server(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STREAMFRAME_DATA_DIR", str(tmp_path / "data"))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")

    code = run_publish(parse("run", "--file", str(clip), "--caption", "hello"))

    output = capsys.readouterr().out
    assert code == 0
    assert "planned" in output
    assert "Nothing was published" in output
    # A dry run must not create a ledger row.
    assert not (tmp_path / "data" / "publish" / "publications.sqlite3").exists()


def test_run_refuses_missing_files(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STREAMFRAME_DATA_DIR", str(tmp_path / "data"))
    code = run_publish(parse("run", "--file", str(tmp_path / "nope.mp4")))
    assert code == 1
    assert "Clip not found" in capsys.readouterr().err


def test_doctor_reports_missing_configuration_as_json(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STREAMFRAME_DATA_DIR", str(tmp_path / "data"))

    code = run_publish(parse("doctor", "--json"))

    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert report["platforms"] == ["youtube", "instagram", "tiktok"]
    assert {problem["platform"] for problem in report["problems"]} == {
        "youtube", "instagram", "tiktok",
    }
    assert report["media_ready"] is False


def test_status_is_empty_before_anything_is_published(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STREAMFRAME_DATA_DIR", str(tmp_path / "data"))
    code = run_publish(parse("status"))
    assert code == 0
    assert "No publications yet" in capsys.readouterr().out


def test_setup_writes_env_once_and_reports_the_next_step(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STREAMFRAME_DATA_DIR", str(tmp_path / "data"))

    code = run_publish(parse("setup", "--write-env"))

    output = capsys.readouterr().out
    env_file = tmp_path / ".env"
    assert code == 1                     # nothing is configured yet
    assert env_file.is_file()
    assert "STREAMFRAME_YOUTUBE_CLIENT_ID" in env_file.read_text()
    assert "console.cloud.google.com" in output
    assert "publish auth youtube" in output

    env_file.write_text("STREAMFRAME_YOUTUBE_CLIENT_ID=keepme\n", encoding="utf-8")
    run_publish(parse("setup", "--write-env"))
    assert "already exists" in capsys.readouterr().out
    assert env_file.read_text() == "STREAMFRAME_YOUTUBE_CLIENT_ID=keepme\n"


def test_a_real_run_refuses_when_nothing_is_configured(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STREAMFRAME_DATA_DIR", str(tmp_path / "data"))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")

    code = run_publish(parse("run", "--file", str(clip), "--yes"))

    captured = capsys.readouterr()
    assert code == 2
    assert "nothing can be published" in captured.err
    # Refusing must not leave a claim behind.
    assert not (tmp_path / "data" / "publish" / "publications.sqlite3").exists()
