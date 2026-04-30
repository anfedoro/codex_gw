from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastapi import HTTPException

from cgw.text_utils import short_text


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def find_state_db() -> Path | None:
    home = codex_home()
    candidates = sorted(home.glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None
    return candidates[0]


def query_state_db(sql: str, args: tuple = ()) -> tuple[Path, list[sqlite3.Row]]:
    state_db = find_state_db()
    if state_db is None:
        raise HTTPException(status_code=404, detail="Codex state DB not found")
    conn = sqlite3.connect(state_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = list(conn.execute(sql, args))
    finally:
        conn.close()
    return state_db, rows


def thread_display_name(cwd: str | None, title: str, first_msg: str, thread_id: str) -> str:
    project = Path(cwd).name if cwd else "project"
    base = title or first_msg or thread_id
    return f"{project}: {short_text(base, 90)}"

