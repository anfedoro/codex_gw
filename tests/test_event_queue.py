import codex_gateway
from fastapi.responses import JSONResponse


def _job(job_id: str, thread_id: str | None, event_seq: int, drained: int) -> dict:
    return {
        "job_id": job_id,
        "status": "running",
        "thread_id": thread_id,
        "event_seq": event_seq,
        "last_drained_seq": drained,
        "events": [],
    }


def test_append_job_event_increments_sequence() -> None:
    job = _job("j1", "t1", 0, 0)
    codex_gateway._append_job_event(job, {"method": "item/completed", "params": {"x": 1}})
    codex_gateway._append_job_event(job, {"method": "turn/completed", "params": {}})

    assert job["event_seq"] == 2
    assert len(job["events"]) == 2
    assert job["events"][0]["seq"] == 1
    assert job["events"][1]["seq"] == 2
    assert codex_gateway._job_pending_events_count(job) == 2


def test_thread_pending_jobs_are_strictly_isolated() -> None:
    old_jobs = codex_gateway.JOBS
    try:
        codex_gateway.JOBS = {
            "a": _job("a", "thread_A", 3, 1),  # pending=2
            "b": _job("b", "thread_B", 5, 5),  # pending=0
            "c": _job("c", "thread_A", 1, 1),  # pending=0
        }
        pending_a = codex_gateway._thread_pending_jobs("thread_A")
        pending_b = codex_gateway._thread_pending_jobs("thread_B")

        assert len(pending_a) == 1
        assert pending_a[0]["job_id"] == "a"
        assert pending_a[0]["pending_events_count"] == 2
        assert pending_b == []
    finally:
        codex_gateway.JOBS = old_jobs


def test_thread_pending_jobs_ignore_terminal_jobs() -> None:
    old_jobs = codex_gateway.JOBS
    try:
        codex_gateway.JOBS = {
            "done_with_tail": _job("done_with_tail", "thread_A", 7, 3),
            "active": _job("active", "thread_A", 6, 4),
        }
        codex_gateway.JOBS["done_with_tail"]["status"] = "completed"
        codex_gateway.JOBS["active"]["status"] = "running"

        pending = codex_gateway._thread_pending_jobs("thread_A")

        assert len(pending) == 1
        assert pending[0]["job_id"] == "active"
        assert pending[0]["status"] == "running"
    finally:
        codex_gateway.JOBS = old_jobs


def test_assert_job_thread_mismatch_raises() -> None:
    job = _job("j1", "thread_A", 0, 0)
    try:
        codex_gateway._assert_job_thread(job, "thread_B")
        assert False, "Expected thread mismatch exception"
    except Exception as exc:
        assert "Thread mismatch" in str(getattr(exc, "detail", exc))


def test_terminal_notice_is_reported_once_and_then_auto_drained() -> None:
    old_jobs = codex_gateway.JOBS
    old_notices = codex_gateway.TERMINAL_JOB_NOTICES
    try:
        codex_gateway.JOBS = {
            "job_done": {
                "job_id": "job_done",
                "status": "completed",
                "thread_id": "thread_A",
                "event_seq": 5,
                "last_drained_seq": 2,
                "events": [{"seq": 3}, {"seq": 4}, {"seq": 5}],
            }
        }
        codex_gateway.TERMINAL_JOB_NOTICES = {}
        codex_gateway._register_terminal_job_notice(codex_gateway.JOBS["job_done"])

        response = JSONResponse({"status": "ok"})
        enriched = codex_gateway._attach_terminal_notices_to_response("/healthz", response)
        payload = enriched.body.decode("utf-8")

        assert "gateway_notifications" in payload
        assert "job_done" in payload
        assert codex_gateway.JOBS["job_done"]["last_drained_seq"] == 5
        assert codex_gateway.JOBS["job_done"]["events"] == []
        assert codex_gateway.TERMINAL_JOB_NOTICES == {}
    finally:
        codex_gateway.JOBS = old_jobs
        codex_gateway.TERMINAL_JOB_NOTICES = old_notices
