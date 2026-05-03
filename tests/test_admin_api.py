from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import codex_gateway
from cgw.models import (
    ProjectCreateRequest,
    SelectThreadModelRequest,
    SwitchProjectContextRequest,
    ThreadCreateRequest,
)


def test_create_project_creates_missing_dir(tmp_path) -> None:
    target = tmp_path / "new_project_dir"
    assert not target.exists()

    payload = asyncio.run(
        codex_gateway.create_project(ProjectCreateRequest(cwd=str(target)))
    )
    assert payload["status"] == "ok"
    assert payload["created"] is True
    assert payload["project"]["cwd"] == str(target.resolve())
    assert Path(payload["project"]["cwd"]).is_dir()


def test_create_project_rejects_file_path(tmp_path) -> None:
    target = tmp_path / "not_a_dir"
    target.write_text("x", encoding="utf-8")

    with pytest.raises(codex_gateway.HTTPException) as exc:
        asyncio.run(codex_gateway.create_project(ProjectCreateRequest(cwd=str(target))))
    assert exc.value.status_code == 400
    assert "not a directory" in str(exc.value.detail)


def test_create_project_validates_existing_dir(tmp_path) -> None:
    target = tmp_path / "existing_project"
    target.mkdir(parents=True, exist_ok=True)

    payload = asyncio.run(
        codex_gateway.create_project(ProjectCreateRequest(cwd=str(target)))
    )
    assert payload["status"] == "ok"
    assert payload["created"] is False
    assert payload["project"]["cwd"] == str(target.resolve())


def test_create_project_missing_dir_without_create_flag(tmp_path) -> None:
    target = tmp_path / "missing_project"
    with pytest.raises(codex_gateway.HTTPException) as exc:
        asyncio.run(
            codex_gateway.create_project(
                ProjectCreateRequest(cwd=str(target), create_if_missing=False)
            )
        )
    assert exc.value.status_code == 404
    assert "does not exist" in str(exc.value.detail)


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

    monkeypatch.setattr(codex_gateway, "_latest_thread_id_for_cwd", lambda cwd: ("thread_old", "/tmp/state.db"))

    async def fake_execute(payload, on_update, on_server_request):
        raise codex_gateway.HTTPException(
            status_code=502,
            detail={"message": "app-server error", "cause": "no rollout found for thread id thread_old"},
        )

    async def fake_create_thread(payload):
        return {
            "status": "ok",
            "thread_id": "thread_new",
            "thread": {"id": "thread_new", "cwd": payload.cwd},
            "interaction_mode": payload.interaction_mode,
        }

    monkeypatch.setattr(codex_gateway, "_execute_codex_with_updates", fake_execute)
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
    monkeypatch.setattr(codex_gateway, "_thread_cwd_from_db", lambda thread_id: "/tmp/project-x")
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
