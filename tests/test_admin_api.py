from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import codex_gateway
from cgw.models import ProjectCreateRequest, ThreadCreateRequest


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
