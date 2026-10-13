"""The .env loader in common/__init__.py."""

import os

from common import load_env


def test_load_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# c\nRUNWAY_T1=a\nexport RUNWAY_T2="b c"\nRUNWAY_T3=x\nnot a line\n', encoding="utf-8")
    monkeypatch.setenv("RUNWAY_T3", "kept")
    for k in ("RUNWAY_T1", "RUNWAY_T2"):
        monkeypatch.setenv(k, "")  # registers the undo, so load_env's values do not leak
        monkeypatch.delenv(k)
    load_env(env)
    assert (os.environ["RUNWAY_T1"], os.environ["RUNWAY_T2"], os.environ["RUNWAY_T3"]) == ("a", "b c", "kept")
