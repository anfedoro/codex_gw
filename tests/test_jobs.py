from cgw.jobs import format_job_view, is_significant_ws_event, parse_unified_diff, prune_jobs


def _to_iso_stub(ts):
    if ts is None:
        return None
    return f"iso-{int(ts)}"


def test_format_job_view_basic_fields() -> None:
    job = {
        "job_id": "job_1",
        "status": "running",
        "created_at": 100,
        "started_at": 101,
        "updated_at": 102,
        "completed_at": None,
        "next_poll_after": 9999999999,
        "thread_id": "t1",
        "approval_policies": [],
        "diff_live_version": 0,
    }
    view = format_job_view(job, poll_after_seconds=15, to_iso=_to_iso_stub)
    assert view["job_id"] == "job_1"
    assert view["thread_id"] == "t1"
    assert view["poll_after_seconds"] == 15
    assert view["created_at"] == "iso-100"


def test_prune_jobs_removes_old_completed() -> None:
    jobs = {
        "old_done": {"job_id": "old_done", "status": "completed", "updated_at": 1},
        "active": {"job_id": "active", "status": "running", "updated_at": 9999999999},
    }
    prune_jobs(jobs, ttl_seconds=10, max_items=10)
    assert "active" in jobs
    assert "old_done" not in jobs


def test_is_significant_ws_event_filter() -> None:
    assert is_significant_ws_event("turn/completed", {}) is True
    assert is_significant_ws_event("item/completed", {"params": {"item": {"type": "agent_message"}}}) is True
    assert is_significant_ws_event("item/completed", {"params": {"item": {"type": "tool_call"}}}) is False
    assert is_significant_ws_event("item/agentMessage/delta", {}) is False


def test_parse_unified_diff_minimal() -> None:
    raw = (
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    files = parse_unified_diff(raw)
    assert len(files) == 1
    assert files[0]["old_path"] == "a/a.txt"
    assert files[0]["new_path"] == "b/a.txt"
    assert files[0]["hunks"][0]["old_lines"] == ["old"]
    assert files[0]["hunks"][0]["new_lines"] == ["new"]

