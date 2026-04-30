from __future__ import annotations

from pathlib import Path


def clip_text(value: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0 or len(value) <= max_chars:
        return value, False
    return value[:max_chars], True


def tail_file(path: Path, max_lines: int) -> list[str]:
    data = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if max_lines <= 0:
        return []
    return data[-max_lines:]


def single_line(text: str) -> str:
    return " ".join((text or "").split())


def short_text(text: str, limit: int = 120) -> str:
    t = single_line(text)
    if len(t) <= limit:
        return t
    return t[: limit - 1].rstrip() + "…"

