from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TaskRequest(BaseModel):
    text: str = ""


class CodexRequest(BaseModel):
    backend: Literal["exec", "app_server_ws"] = "exec"
    kind: Literal["new", "resume"] = "new"
    session_id: str | None = None
    prompt: str | None = None
    cwd: str | None = None
    profile: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    sandbox: str | None = None
    approvals: str | None = None
    search: bool = False
    use_exec: bool = Field(
        default=True,
        description="Use `codex exec` (recommended for non-interactive gateway usage).",
    )
    app_server_url: str | None = None
    app_server_bearer_token: str | None = None
    include_events: bool = Field(
        default=False,
        description="Include raw app-server events in response for debugging.",
    )
    max_events: int = Field(
        default=50,
        ge=0,
        le=500,
        description="Maximum number of app-server events to keep when include_events=true.",
    )
    diff_mode: Literal["live", "final_only", "off"] = Field(
        default="live",
        description="Diff orchestration mode for async jobs.",
    )


class CodexJobRequest(BaseModel):
    payload: CodexRequest


class CodexJobApprovalRequest(BaseModel):
    request_id: int | str
    decision: Literal["approve_once", "approve_all_similar", "deny", "guidance"]
    guidance_text: str | None = None
    scope_hint: str | None = None


class ProjectCreateRequest(BaseModel):
    cwd: str
    create_if_missing: bool = True


class ThreadCreateRequest(BaseModel):
    cwd: str | None = None
    model: str | None = None
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] | None = None
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] | None = None
    approvals: str | None = None
    app_server_url: str | None = None
    app_server_bearer_token: str | None = None
    interaction_mode: Literal["execution", "planning"] = "execution"


class SwitchProjectContextRequest(BaseModel):
    project_path: str
    thread_policy: Literal["reuse_latest", "create_new"] = "reuse_latest"
    create_project_if_missing: bool = True
    model: str | None = None
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] | None = None
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] | None = None
    approvals: str | None = None
    app_server_url: str | None = None
    app_server_bearer_token: str | None = None
    interaction_mode: Literal["execution", "planning"] = "execution"


class SelectThreadModelRequest(BaseModel):
    thread_id: str
    model: str
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] | None = None
    interaction_mode: Literal["execution", "planning"] = "execution"
    app_server_url: str | None = None
    app_server_bearer_token: str | None = None


class SkillsListRequest(BaseModel):
    cwd: str | None = None
    force_reload: bool = False


class SkillConfigWriteRequest(BaseModel):
    enabled: bool
    name: str | None = None
    path: str | None = None


class SkillInvokeRequest(BaseModel):
    thread_id: str
    skill_name: str | None = None
    skill_path: str | None = None
    text: str | None = None
    model: str | None = None
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] | None = None
    cwd: str | None = None
    app_server_url: str | None = None
    app_server_bearer_token: str | None = None
