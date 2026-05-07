from __future__ import annotations

import asyncio

import pytest

import codex_gateway
from cgw.models import (
    CodexJobRequest,
    CodexRequest,
    ProjectCreateRequest,
    SkillConfigWriteRequest,
    SkillInvokeRequest,
    SelectThreadModelRequest,
    SwitchProjectContextRequest,
    ThreadCreateRequest,
)


def test_create_project_is_codex_only_and_does_not_touch_fs(tmp_path, monkeypatch) -> None:
    target = tmp_path / "new_project_dir"
    calls: list[tuple[str, dict]] = []

    async def fake_rpc(method, params=None, **kwargs):
        calls.append((method, params or {}))
        if method == "config/read":
            return {"config": {}, "origins": {}}
        if method == "thread/list":
            return {"data": []}
        raise AssertionError(method)

    monkeypatch.setattr(codex_gateway, "_app_server_rpc", fake_rpc)
    payload = asyncio.run(codex_gateway.create_project(ProjectCreateRequest(cwd=str(target))))
    assert payload["status"] == "ok"
    assert payload["created"] is False
    assert payload["project"]["cwd"] == str(target.resolve())
    assert calls[0][0] == "config/read"
    assert calls[1][0] == "thread/list"
    assert not target.exists()


def test_create_thread_uses_context_mode_and_model(monkeypatch) -> None:
    captured: dict = {}

    async def fake_execute(payload, on_update, on_server_request):
        captured["payload"] = payload
        return {
            "backend": "app_server_ws",
            "app_server_url": "ws://127.0.0.1:4500",
            "thread": {
                "id": "thread_123",
                "cwd": payload.cwd,
                "model": payload.model,
                "modelReasoningEffort": payload.reasoning_effort,
            },
        }

    monkeypatch.setattr(codex_gateway, "_execute_codex_with_updates", fake_execute)

    body = asyncio.run(
        codex_gateway.create_thread(
            ThreadCreateRequest(
                cwd="/tmp",
                model="gpt-5.5",
                reasoning_effort="high",
                interaction_mode="planning",
            )
        )
    )
    assert body["status"] == "ok"
    assert body["thread_id"] == "thread_123"
    assert body["interaction_mode"] == "planning"
    assert captured["payload"].model == "gpt-5.5"
    assert captured["payload"].reasoning_effort == "high"
    assert captured["payload"].prompt is None


def test_list_available_models_parses_runtime_catalog(monkeypatch) -> None:
    monkeypatch.setattr(
        codex_gateway,
        "_run_command",
        lambda cmd, cwd, timeout: {
            "exit_code": 0,
            "stdout": (
                "WARNING: something\n"
                '{"models":[{"slug":"gpt-5.5","display_name":"GPT-5.5",'
                '"default_reasoning_level":"medium",'
                '"supported_reasoning_levels":[{"effort":"low"},{"effort":"high"}],'
                '"supported_in_api":true,"visibility":"list","priority":1}]}'
            ),
            "stderr": "",
        },
    )
    body = asyncio.run(codex_gateway.list_available_models())
    assert body["status"] == "ok"
    assert body["count"] == 1
    assert body["models"][0]["id"] == "gpt-5.5"
    assert body["models"][0]["supported_reasoning_efforts"] == ["low", "high"]


def test_switch_project_context_falls_back_to_new_thread_on_missing_rollout(tmp_path, monkeypatch) -> None:
    project = tmp_path / "proj"

    async def fake_latest(cwd, **kwargs):
        return "thread_old"

    async def fake_rpc(method, params=None, **kwargs):
        if method == "thread/resume":
            raise codex_gateway.HTTPException(
                status_code=502,
                detail={"message": "app-server error", "cause": "no rollout found for thread id thread_old"},
            )
        raise AssertionError(method)

    async def fake_create_thread(payload):
        return {
            "status": "ok",
            "thread_id": "thread_new",
            "thread": {"id": "thread_new", "cwd": payload.cwd},
            "interaction_mode": payload.interaction_mode,
        }

    async def fake_create_project(payload):
        return {
            "status": "ok",
            "created": False,
            "project": {"cwd": str(project.resolve()), "name": "proj", "thread_count": 0, "last_updated_at": None},
        }

    monkeypatch.setattr(codex_gateway, "_latest_thread_id_for_cwd", fake_latest)
    monkeypatch.setattr(codex_gateway, "_app_server_rpc", fake_rpc)
    monkeypatch.setattr(codex_gateway, "create_project", fake_create_project)
    monkeypatch.setattr(codex_gateway, "create_thread", fake_create_thread)

    body = asyncio.run(
        codex_gateway.switch_project_context(
            SwitchProjectContextRequest(
                project_path=str(project),
                thread_policy="reuse_latest",
            )
        )
    )
    assert body["status"] == "ok"
    assert body["thread_status"] == "created_new"
    assert body["thread_id"] == "thread_new"
    assert body["stale_thread_id"] == "thread_old"


def test_switch_project_context_resumes_with_limited_turns_on_payload_overflow(tmp_path, monkeypatch) -> None:
    project = tmp_path / "proj"

    async def fake_latest(cwd, **kwargs):
        return "thread_old"

    async def fake_create_project(payload):
        return {
            "status": "ok",
            "created": False,
            "project": {"cwd": str(project.resolve()), "name": "proj", "thread_count": 0, "last_updated_at": None},
        }

    async def fake_rpc(method, params=None, **kwargs):
        if method == "thread/resume" and params and params.get("excludeTurns") is False:
            raise codex_gateway.HTTPException(
                status_code=502,
                detail="App-server websocket receive failed: sent 1009 (message too big) frame with 1300000 bytes",
            )
        if method == "thread/resume" and params and params.get("excludeTurns") is True:
            return {"thread": {"id": "thread_old", "cwd": str(project.resolve())}}
        if method == "thread/turns/list":
            assert params["threadId"] == "thread_old"
            assert params["limit"] == 10
            return {"data": [{"id": "turn_1"}, {"id": "turn_2"}]}
        raise AssertionError((method, params))

    monkeypatch.setattr(codex_gateway, "_latest_thread_id_for_cwd", fake_latest)
    monkeypatch.setattr(codex_gateway, "_app_server_rpc", fake_rpc)
    monkeypatch.setattr(codex_gateway, "create_project", fake_create_project)

    body = asyncio.run(
        codex_gateway.switch_project_context(
            SwitchProjectContextRequest(
                project_path=str(project),
                thread_policy="reuse_latest",
            )
        )
    )
    assert body["status"] == "ok"
    assert body["thread_status"] == "resumed"
    assert body["thread_id"] == "thread_old"
    assert body["resume_context_mode"] == "limited_last_10_turns"
    assert isinstance(body["thread"]["turns"], list)
    assert len(body["thread"]["turns"]) == 2


def test_create_codex_job_requires_resumed_thread_context() -> None:
    with pytest.raises(codex_gateway.HTTPException) as exc:
        asyncio.run(
            codex_gateway.create_codex_job(
                CodexJobRequest(
                    payload=CodexRequest(
                        backend="app_server_ws",
                        kind="new",
                        prompt="Do task",
                        cwd="/tmp",
                    )
                )
            )
        )
    assert exc.value.status_code == 409
    assert "active thread context" in str(exc.value.detail)


def test_get_codex_job_returns_heartbeat_and_progress_delta() -> None:
    old_jobs = codex_gateway.JOBS
    old_long_poll = codex_gateway.JOB_LONG_POLL_ENABLED
    now = codex_gateway.job_now()
    try:
        codex_gateway.JOB_LONG_POLL_ENABLED = False
        codex_gateway.JOBS = {
            "job_1": {
                "job_id": "job_1",
                "status": "running",
                "created_at": now - 20,
                "started_at": now - 18,
                "updated_at": now - 1,
                "completed_at": None,
                "thread_id": "thread_1",
                "next_poll_after": now + 30,
                "last_event_method": "job/running",
                "last_update_text": "Working...",
                "approval_required": False,
                "approval_request": None,
                "approval_policies": [],
                "diff_mode": "off",
                "diff_live_available": False,
                "diff_live_version": 0,
                "diff_final_available": False,
                "diff_hint": None,
                "diagnostic_diff_available": False,
                "event_seq": 0,
                "last_drained_seq": 0,
                "progress_seq": 2,
                "progress_items": [
                    {"seq": 1, "kind": "job/running", "elapsed_sec": 0, "elapsed_label": "0s", "text": "started"},
                    {"seq": 2, "kind": "turn/completed", "elapsed_sec": 10, "elapsed_label": "10s", "text": "step"},
                ],
                "notify_event": asyncio.Event(),
                "done_event": asyncio.Event(),
                "error": None,
                "result": None,
            }
        }

        fresh = asyncio.run(
            codex_gateway.get_codex_job(
                "job_1",
                thread_id=None,
                since_progress_seq=0,
                max_progress_items=1,
                wait_seconds=0,
            )
        )
        assert fresh["progress_delta"]["current_seq"] == 2
        assert len(fresh["progress_delta"]["items"]) == 1
        assert fresh["progress_delta"]["truncated"] is True
        assert fresh["heartbeat"] is None

        idle = asyncio.run(
            codex_gateway.get_codex_job(
                "job_1",
                thread_id=None,
                since_progress_seq=2,
                max_progress_items=5,
                wait_seconds=0,
            )
        )
        assert idle["progress_delta"]["items"] == []
        assert idle["heartbeat"]["alive"] is True
        assert idle["heartbeat"]["status"] == "running"
    finally:
        codex_gateway.JOB_LONG_POLL_ENABLED = old_long_poll
        codex_gateway.JOBS = old_jobs


def test_select_model_for_thread_creates_new_thread_with_selected_model(monkeypatch) -> None:
    async def fake_list_models():
        return {
            "status": "ok",
            "count": 2,
            "models": [{"id": "gpt-5.5"}, {"id": "gpt-5.4"}],
        }

    async def fake_create_thread(payload):
        return {
            "status": "ok",
            "thread_id": "thread_new_model",
            "thread": {"id": "thread_new_model", "cwd": payload.cwd, "model": payload.model},
            "interaction_mode": payload.interaction_mode,
        }

    monkeypatch.setattr(codex_gateway, "list_available_models", fake_list_models)
    async def fake_thread_cwd(thread_id, **kwargs):
        return "/tmp/project-x"

    monkeypatch.setattr(codex_gateway, "_thread_cwd_from_runtime", fake_thread_cwd)
    monkeypatch.setattr(codex_gateway, "create_thread", fake_create_thread)

    body = asyncio.run(
        codex_gateway.select_model_for_thread(
            SelectThreadModelRequest(
                thread_id="thread_old",
                model="gpt-5.5",
                reasoning_effort="high",
                interaction_mode="planning",
            )
        )
    )
    assert body["status"] == "ok"
    assert body["previous_thread_id"] == "thread_old"
    assert body["thread_id"] == "thread_new_model"
    assert body["selected_model"] == "gpt-5.5"
    assert body["interaction_mode"] == "planning"


def test_list_skills_calls_codex_skills_list(monkeypatch) -> None:
    async def fake_rpc(method, params=None, **kwargs):
        assert method == "skills/list"
        assert params["forceReload"] is True
        return {
            "data": [
                {
                    "cwd": "/tmp/p",
                    "errors": [],
                    "skills": [{"name": "skill-a", "path": "/tmp/p/skills/a"}],
                }
            ]
        }

    monkeypatch.setattr(codex_gateway, "_app_server_rpc", fake_rpc)
    body = asyncio.run(codex_gateway.list_skills(cwd="/tmp/p", force_reload=True))
    assert body["status"] == "ok"
    assert body["count"] == 1
    assert body["data"][0]["cwd"] == "/tmp/p"


def test_write_skill_config_requires_selector() -> None:
    with pytest.raises(codex_gateway.HTTPException) as exc:
        asyncio.run(codex_gateway.write_skill_config(SkillConfigWriteRequest(enabled=True)))
    assert exc.value.status_code == 400


def test_invoke_skill_resolves_by_name_and_starts_turn(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_thread_cwd(thread_id, **kwargs):
        return "/tmp/project-z"

    async def fake_rpc(method, params=None, **kwargs):
        calls.append((method, params or {}))
        if method == "skills/list":
            return {
                "data": [
                    {
                        "cwd": "/tmp/project-z",
                        "errors": [],
                        "skills": [{"name": "skill-x", "path": "/tmp/project-z/skills/skill-x"}],
                    }
                ]
            }
        if method == "turn/start":
            assert params["threadId"] == "thread_1"
            assert params["input"][0]["type"] == "skill"
            assert params["input"][0]["name"] == "skill-x"
            return {"turn": {"id": "turn_1", "status": "started"}}
        raise AssertionError(method)

    monkeypatch.setattr(codex_gateway, "_thread_cwd_from_runtime", fake_thread_cwd)
    monkeypatch.setattr(codex_gateway, "_app_server_rpc", fake_rpc)

    body = asyncio.run(
        codex_gateway.invoke_skill(
            SkillInvokeRequest(
                thread_id="thread_1",
                skill_name="skill-x",
                text="Run it",
            )
        )
    )
    assert body["status"] == "ok"
    assert body["skill_name"] == "skill-x"
    assert body["turn"]["id"] == "turn_1"
    assert calls[0][0] == "skills/list"
    assert calls[1][0] == "turn/start"
