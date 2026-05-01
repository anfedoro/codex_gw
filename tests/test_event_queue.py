import codex_gateway


def _job(job_id: str, thread_id: str | None, event_seq: int, drained: int) -> dict:
    return {
        "job_id": job_id,
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


def test_assert_job_thread_mismatch_raises() -> None:
    job = _job("j1", "thread_A", 0, 0)
    try:
        codex_gateway._assert_job_thread(job, "thread_B")
        assert False, "Expected thread mismatch exception"
    except Exception as exc:
        assert "Thread mismatch" in str(getattr(exc, "detail", exc))
