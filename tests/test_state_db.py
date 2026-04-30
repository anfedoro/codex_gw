import sqlite3
from pathlib import Path

from cgw.state_db import find_state_db, query_state_db, thread_display_name


def test_thread_display_name_uses_project_and_short_text() -> None:
    name = thread_display_name("/tmp/myproj", "Very long title", "", "thread-1")
    assert name.startswith("myproj: ")


def test_find_state_db_and_query(tmp_path: Path, monkeypatch) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    state_db = codex_home / "state_1.sqlite"

    conn = sqlite3.connect(state_db)
    conn.execute("CREATE TABLE threads (id TEXT, archived INTEGER, cwd TEXT)")
    conn.execute("INSERT INTO threads (id, archived, cwd) VALUES ('t1', 0, '/tmp/p')")
    conn.commit()
    conn.close()

    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    found = find_state_db()
    assert found == state_db

    db_path, rows = query_state_db("SELECT id FROM threads")
    assert db_path == state_db
    assert len(rows) == 1
    assert rows[0]["id"] == "t1"

