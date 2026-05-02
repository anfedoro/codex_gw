from __future__ import annotations

import time
from typing import Callable


def job_now() -> int:
    return int(time.time())


def format_job_view(
    job: dict,
    *,
    poll_after_seconds: int,
    to_iso: Callable[[int | None], str | None],
    include_result: bool = False,
) -> dict:
    now = job_now()
    next_poll_after = int(job.get("next_poll_after", 0) or 0)
    retry_after_seconds = max(0, next_poll_after - now)
    out = {
        "job_id": job["job_id"],
        "status": job["status"],
        "created_at": to_iso(job.get("created_at")),
        "started_at": to_iso(job.get("started_at")),
        "updated_at": to_iso(job.get("updated_at")),
        "completed_at": to_iso(job.get("completed_at")),
        "poll_after_seconds": poll_after_seconds,
        "next_poll_after_at": to_iso(next_poll_after) if next_poll_after > 0 else None,
        "retry_after_seconds": retry_after_seconds,
        "thread_id": job.get("thread_id"),
        "last_event_method": job.get("last_event_method"),
        "last_update_text": job.get("last_update_text"),
        "approval_required": bool(job.get("approval_required", False)),
        "approval_request": job.get("approval_request"),
        "approval_policy_count": len(job.get("approval_policies", [])),
        "diff_mode": job.get("diff_mode"),
        "diff_live_available": bool(job.get("diff_live_available", False)),
        "diff_live_version": int(job.get("diff_live_version", 0) or 0),
        "diff_final_available": bool(job.get("diff_final_available", False)),
        "diff_hint": job.get("diff_hint"),
        "diagnostic_diff_available": bool(job.get("diagnostic_diff_available", False)),
        "event_seq": int(job.get("event_seq", 0) or 0),
        "last_drained_seq": int(job.get("last_drained_seq", 0) or 0),
        "pending_events_count": max(
            0,
            int(job.get("event_seq", 0) or 0) - int(job.get("last_drained_seq", 0) or 0),
        ),
        "progress_summary": {
            "items": list(job.get("progress_items", [])[-5:]),
        },
        "error": job.get("error"),
    }
    if include_result:
        out["result"] = job.get("result")
    return out


def prune_jobs(jobs: dict[str, dict], *, ttl_seconds: int, max_items: int) -> None:
    now = job_now()
    drop_ids = []
    for job_id, job in jobs.items():
        done = job["status"] in {"completed", "failed"}
        is_expired = done and (now - int(job.get("updated_at", now)) > ttl_seconds)
        if is_expired:
            drop_ids.append(job_id)
    for job_id in drop_ids:
        jobs.pop(job_id, None)
    if len(jobs) <= max_items:
        return
    overflow = len(jobs) - max_items
    oldest = sorted(jobs.values(), key=lambda j: int(j.get("updated_at", 0)))[:overflow]
    for job in oldest:
        jobs.pop(job["job_id"], None)


def is_significant_ws_event(method: str | None, msg: dict) -> bool:
    if not method:
        return False
    if method in {"turn/completed", "item/completed", "error"}:
        if method == "item/completed":
            item = msg.get("params", {}).get("item", {})
            return item.get("type") == "agent_message"
        return True
    return False


def parse_unified_diff(raw: str) -> list[dict]:
    files: list[dict] = []
    current_file: dict | None = None
    current_hunk: dict | None = None

    for line in raw.splitlines():
        if line.startswith("diff --git "):
            current_file = {"old_path": None, "new_path": None, "hunks": []}
            files.append(current_file)
            current_hunk = None
            continue
        if current_file is None:
            continue
        if line.startswith("--- "):
            current_file["old_path"] = line[4:].strip()
            continue
        if line.startswith("+++ "):
            current_file["new_path"] = line[4:].strip()
            continue
        if line.startswith("@@ "):
            current_hunk = {"header": line, "old_lines": [], "new_lines": [], "context_lines": []}
            current_file["hunks"].append(current_hunk)
            continue
        if current_hunk is None:
            continue
        if line.startswith("+") and not line.startswith("+++ "):
            current_hunk["new_lines"].append(line[1:])
        elif line.startswith("-") and not line.startswith("--- "):
            current_hunk["old_lines"].append(line[1:])
        else:
            txt = line[1:] if line.startswith(" ") else line
            current_hunk["context_lines"].append(txt)
    return files
