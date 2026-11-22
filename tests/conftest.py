"""Keep a developer's real .env out of test outcomes.

Publishing configuration is normally discovered from the current directory's
``.env``. Once a checkout has a filled-in ``.env``, tests that build a
``Settings`` would read real credentials and change their results. Point
discovery at an empty file instead; a test that exercises discovery itself
deletes ``STREAMFRAME_PUBLISH_ENV`` first.

The file lives in the session's temp root, not in each test's ``tmp_path``,
because several tests assert that ``tmp_path`` stays empty.
"""

import pytest


@pytest.fixture(scope="session")
def _empty_env(tmp_path_factory):
    path = tmp_path_factory.mktemp("publishing-env") / "empty.env"
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def isolate_publishing_env(_empty_env, monkeypatch):
    monkeypatch.setenv("STREAMFRAME_PUBLISH_ENV", str(_empty_env))
