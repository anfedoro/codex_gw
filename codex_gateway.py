#!/usr/bin/env python3
"""
FastAPI gateway for repository introspection and Codex CLI execution.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


app = FastAPI(title="Codex Gateway", version="1.0.0")
LOGGER = logging.getLogger("codex_gateway")

# Mutable runtime config, initialized from env and overridden by CLI args.
REPO = Path(os.environ.get("REPO", os.getcwd())).resolve()
CODEX_TIMEOUT_SECONDS = int(os.environ.get("CODEX_TIMEOUT_SECONDS", "120"))
APP_SERVER_URL = os.environ.get("APP_SERVER_URL", "ws://127.0.0.1:4500")
APP_SERVER_BEARER_TOKEN = os.environ.get("APP_SERVER_BEARER_TOKEN")
APP_SERVER_TIMEOUT_SECONDS = int(os.environ.get("APP_SERVER_TIMEOUT_SECONDS", "180"))
GATEWAY_API_KEY = os.environ.get("CODEX_GATEWAY_API_KEY", "").strip()
GATEWAY_API_KEY_HEADER = os.environ.get("GATEWAY_API_KEY_HEADER", "x-api-key").strip().lower()
AUTH_DISABLED = os.environ.get("GATEWAY_DISABLE_AUTH", "0") == "1"
DEBUG_MODE = os.environ.get("GATEWAY_DEBUG", "0") == "1"
LOG_LEVEL = os.environ.get("GATEWAY_LOG_LEVEL", "INFO").upper()
LOG_FILE = os.environ.get("GATEWAY_LOG_FILE", "").strip()
LOG_REQUESTS = os.environ.get("GATEWAY_LOG_REQUESTS", "1") == "1"
MAX_OUTPUT_CHARS = int(os.environ.get("GATEWAY_MAX_OUTPUT_CHARS", "60000"))
MANAGED_APP_SERVER_PROCESS: subprocess.Popen[str] | None = None
MANAGED_APP_SERVER_LISTEN_URL: str | None = None
MANAGED_APP_SERVER_STARTED_BY_GATEWAY = False


def _configure_logging() -> None:
    level_name = "DEBUG" if DEBUG_MODE else LOG_LEVEL
    level = getattr(logging, level_name, logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if LOG_FILE:
        log_path = Path(LOG_FILE).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )
    LOGGER.info(
        "logging configured level=%s debug=%s log_file=%s",
        logging.getLevelName(level),
        DEBUG_MODE,
        LOG_FILE or "<stdout-only>",
    )


def _clip_text(value: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0 or len(value) <= max_chars:
        return value, False
    return value[:max_chars], True


def _tail_file(path: Path, max_lines: int) -> list[str]:
    data = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if max_lines <= 0:
        return []
    return data[-max_lines:]


def _extract_bearer_token(authorization_header: str | None) -> str | None:
    if not authorization_header:
        return None
    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


def _extract_api_key_from_request(request: Request) -> str | None:
    bearer = _extract_bearer_token(request.headers.get("Authorization"))
    if bearer:
        return bearer
    if GATEWAY_API_KEY_HEADER:
        raw = request.headers.get(GATEWAY_API_KEY_HEADER)
        if raw:
            return raw.strip()
    return None


def _token_fingerprint(token: str | None) -> str:
    if not token:
        return "none"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    return f"len={len(token)} sha256_12={digest}"


def _start_managed_app_server(codex_bin: str, listen_url: str) -> None:
    global MANAGED_APP_SERVER_PROCESS, MANAGED_APP_SERVER_LISTEN_URL, MANAGED_APP_SERVER_STARTED_BY_GATEWAY
    if MANAGED_APP_SERVER_PROCESS and MANAGED_APP_SERVER_PROCESS.poll() is None:
        return
    cmd = [codex_bin, "app-server", "--listen", listen_url]
    LOGGER.info("starting managed app-server: %s", " ".join(cmd))
    proc = subprocess.Popen(cmd, text=True)
    time.sleep(0.5)
    if proc.poll() is not None:
        LOGGER.error("managed app-server exited early code=%s", proc.returncode)
        raise RuntimeError(f"Managed app-server exited early with code {proc.returncode}")
    MANAGED_APP_SERVER_PROCESS = proc
    MANAGED_APP_SERVER_LISTEN_URL = listen_url
    MANAGED_APP_SERVER_STARTED_BY_GATEWAY = True
    LOGGER.info("managed app-server running pid=%s listen=%s", proc.pid, listen_url)


def _stop_managed_app_server() -> None:
    global MANAGED_APP_SERVER_PROCESS
    proc = MANAGED_APP_SERVER_PROCESS
    if not proc:
        return
    if proc.poll() is None:
        LOGGER.info("stopping managed app-server pid=%s", proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            LOGGER.warning("managed app-server terminate timeout; killing pid=%s", proc.pid)
            proc.kill()
            proc.wait(timeout=5)
    LOGGER.info("managed app-server stopped")
    MANAGED_APP_SERVER_PROCESS = None


@app.middleware("http")
async def require_bearer_api_key(request: Request, call_next):
    started = time.monotonic()
    # Authentication is enabled when CODEX_GATEWAY_API_KEY is set and
    # GATEWAY_DISABLE_AUTH is not set to 1.
    if GATEWAY_API_KEY and not AUTH_DISABLED:
        token = _extract_api_key_from_request(request)
        if not token or not hmac.compare_digest(token, GATEWAY_API_KEY):
            has_auth_header = bool(request.headers.get("Authorization"))
            has_api_key_header = bool(
                GATEWAY_API_KEY_HEADER and request.headers.get(GATEWAY_API_KEY_HEADER)
            )
            LOGGER.warning(
                "unauthorized request method=%s path=%s has_authorization=%s has_api_key_header=%s api_key_header_name=%s incoming_token_fp=%s expected_token_fp=%s",
                request.method,
                request.url.path,
                has_auth_header,
                has_api_key_header,
                GATEWAY_API_KEY_HEADER,
                _token_fingerprint(token),
                _token_fingerprint(GATEWAY_API_KEY),
            )
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    response = await call_next(request)
    if LOG_REQUESTS or DEBUG_MODE:
        duration_ms = (time.monotonic() - started) * 1000.0
        LOGGER.info(
            "request method=%s path=%s status=%s duration_ms=%.1f",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
    return response


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


def _resolve_repo_path(path: str) -> Path:
    target = (REPO / path).resolve()
    try:
        target.relative_to(REPO)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Illegal path") from exc
    return target


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def _find_state_db() -> Path | None:
    home = _codex_home()
    candidates = sorted(home.glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None
    return candidates[0]


def _to_iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC).isoformat()
    except Exception:
        return None


def _single_line(text: str) -> str:
    return " ".join((text or "").split())


def _short_text(text: str, limit: int = 120) -> str:
    t = _single_line(text)
    if len(t) <= limit:
        return t
    return t[: limit - 1].rstrip() + "…"


def _thread_display_name(cwd: str | None, title: str, first_msg: str, thread_id: str) -> str:
    project = Path(cwd).name if cwd else "project"
    base = title or first_msg or thread_id
    return f"{project}: {_short_text(base, 90)}"


def _query_state_db(sql: str, args: tuple = ()) -> tuple[Path, list[sqlite3.Row]]:
    state_db = _find_state_db()
    if state_db is None:
        raise HTTPException(status_code=404, detail="Codex state DB not found")
    conn = sqlite3.connect(state_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = list(conn.execute(sql, args))
    finally:
        conn.close()
    return state_db, rows


def _run_command(cmd: list[str], cwd: Path, timeout: int) -> dict:
    LOGGER.debug("run command cwd=%s timeout=%s cmd=%s", cwd, timeout, cmd)
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        LOGGER.error("command timeout cwd=%s timeout=%s cmd=%s", cwd, timeout, cmd)
        raise HTTPException(status_code=504, detail="Command timed out") from exc
    except Exception as exc:  # pragma: no cover
        LOGGER.exception("command failed to start cwd=%s cmd=%s", cwd, cmd)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    stdout, stdout_truncated = _clip_text(result.stdout, MAX_OUTPUT_CHARS)
    stderr, stderr_truncated = _clip_text(result.stderr, MAX_OUTPUT_CHARS)
    if stdout_truncated or stderr_truncated:
        LOGGER.warning(
            "command output truncated cmd=%s stdout_truncated=%s stderr_truncated=%s max_output_chars=%s",
            cmd,
            stdout_truncated,
            stderr_truncated,
            MAX_OUTPUT_CHARS,
        )
    LOGGER.debug("command completed exit_code=%s cmd=%s", result.returncode, cmd)

    return {
        "command": cmd,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": result.returncode,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }


def _build_codex_command(payload: CodexRequest, cwd: Path) -> list[str]:
    if payload.use_exec:
        cmd: list[str] = ["codex", "exec", "--json", "--cd", str(cwd)]
    else:
        cmd = ["codex", "--cd", str(cwd)]

    if payload.profile:
        cmd.extend(["--profile", payload.profile])
    if payload.model:
        cmd.extend(["--model", payload.model])
    if payload.reasoning_effort:
        cmd.extend(["-c", f"model_reasoning_effort={payload.reasoning_effort}"])
    if payload.sandbox:
        cmd.extend(["--sandbox", payload.sandbox])
    if payload.approvals:
        # `codex exec` compatibility: this CLI version does not support
        # `--ask-for-approval`. Keep behavior safe and explicit.
        approvals = str(payload.approvals).strip().lower()
        if approvals in {"never", "auto", "on-failure"}:
            cmd.append("--full-auto")
            LOGGER.info("mapped approvals=%s to --full-auto for codex exec", approvals)
        elif approvals in {"on-request", "manual"}:
            LOGGER.info("approvals=%s: no extra exec flag required", approvals)
        else:
            LOGGER.warning("unknown approvals value ignored for codex exec: %s", payload.approvals)
    if payload.search:
        cmd.append("--search")

    if payload.kind == "resume":
        cmd.append("resume")
        if not payload.session_id:
            raise HTTPException(status_code=400, detail="session_id required for resume")
        cmd.append(payload.session_id)

    if payload.prompt:
        cmd.append(payload.prompt)
    return cmd


async def _run_codex_via_app_server_ws(payload: CodexRequest, cwd: Path | None) -> dict:
    try:
        from websockets.asyncio.client import connect as ws_connect
    except Exception:
        try:
            from websockets import connect as ws_connect  # type: ignore
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail="websockets package is required for app_server_ws backend",
            ) from exc

    url = payload.app_server_url or APP_SERVER_URL
    token = payload.app_server_bearer_token or APP_SERVER_BEARER_TOKEN
    timeout = APP_SERVER_TIMEOUT_SECONDS
    LOGGER.info("ws backend start url=%s kind=%s cwd=%s", url, payload.kind, cwd)

    connect_kwargs: dict = {}
    if token:
        connect_kwargs["additional_headers"] = {"Authorization": f"Bearer {token}"}

    try:
        ws_ctx = ws_connect(url, **connect_kwargs)
    except TypeError:
        # Compatibility with older websockets APIs.
        connect_kwargs = {}
        if token:
            connect_kwargs["extra_headers"] = {"Authorization": f"Bearer {token}"}
        ws_ctx = ws_connect(url, **connect_kwargs)

    next_id = 1
    thread_id: str | None = None
    turn_response: dict | None = None
    turn_completed = False
    assistant_deltas: list[str] = []
    assistant_completed_text: str | None = None
    events: list[dict] = []

    async def send(ws, obj: dict) -> None:
        if "jsonrpc" not in obj:
            obj = {"jsonrpc": "2.0", **obj}
        await ws.send(json.dumps(obj))

    async def recv(ws) -> dict:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Timeout waiting for app-server message") from exc
        try:
            return json.loads(raw)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Invalid app-server JSON: {raw}") from exc

    async def process_message(ws, msg: dict) -> None:
        nonlocal thread_id, turn_completed, assistant_completed_text
        if payload.include_events and len(events) < payload.max_events:
            events.append(msg)

        method = msg.get("method")
        if method == "thread/started":
            thread = msg.get("params", {}).get("thread", {})
            thread_id = thread.get("id") or thread_id
        elif method == "item/agentMessage/delta":
            delta = msg.get("params", {}).get("delta")
            if isinstance(delta, str):
                assistant_deltas.append(delta)
        elif method == "item/completed":
            item = msg.get("params", {}).get("item", {})
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                assistant_completed_text = item["text"]
        elif method == "turn/completed":
            turn_completed = True

        # Handle server-initiated request (for approvals/user input).
        if "id" in msg and "method" in msg and "result" not in msg and "error" not in msg:
            request_id = msg["id"]
            request_method = msg["method"]
            if request_method in (
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            ):
                LOGGER.warning("auto-decline server approval request method=%s", request_method)
                await send(ws, {"id": request_id, "result": {"decision": "decline"}})
            else:
                LOGGER.warning("unsupported server request method=%s", request_method)
                await send(
                    ws,
                    {
                        "id": request_id,
                        "error": {
                            "code": -32004,
                            "message": f"Unsupported server request in gateway: {request_method}",
                        },
                    },
                )

    async def wait_for_response(ws, request_id: int) -> dict:
        while True:
            msg = await recv(ws)
            await process_message(ws, msg)
            if msg.get("id") == request_id:
                if "error" in msg:
                    raise HTTPException(status_code=502, detail=f"app-server error: {msg['error']}")
                return msg.get("result", {})

    async with ws_ctx as ws:
        # initialize
        init_id = next_id
        next_id += 1
        await send(
            ws,
            {
                "id": init_id,
                "method": "initialize",
                "params": {"clientInfo": {"name": "codex-gateway", "version": "1.0.0"}},
            },
        )
        init_result = await wait_for_response(ws, init_id)

        # initialized notification
        await send(ws, {"method": "initialized", "params": {}})

        # thread start/resume
        thread_req_id = next_id
        next_id += 1
        if payload.kind == "resume":
            if not payload.session_id:
                raise HTTPException(status_code=400, detail="session_id required for resume")
            thread_req = {
                "id": thread_req_id,
                "method": "thread/resume",
                "params": {"threadId": payload.session_id},
            }
        else:
            params: dict = {}
            if cwd is not None:
                params["cwd"] = str(cwd)
            if payload.model:
                params["model"] = payload.model
            if payload.sandbox:
                params["sandbox"] = payload.sandbox
            if payload.approvals:
                params["approvalPolicy"] = payload.approvals
            thread_req = {"id": thread_req_id, "method": "thread/start", "params": params}

        await send(ws, thread_req)

        try:
            thread_result = await wait_for_response(ws, thread_req_id)
        except HTTPException as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={
                    "message": "Failed waiting thread start/resume response from app-server",
                    "request_id": thread_req_id,
                    "events_seen": len(events),
                    "last_event": events[-1] if events else None,
                    "cause": exc.detail,
                },
            ) from exc
        thread = thread_result.get("thread", {})
        thread_id = thread.get("id") or thread_id
        if not thread_id:
            raise HTTPException(status_code=502, detail="app-server did not return thread id")
        LOGGER.info("ws thread started thread_id=%s", thread_id)

        # If no prompt was supplied, return thread context only.
        if not payload.prompt:
            output = {
                "backend": "app_server_ws",
                "app_server_url": url,
                "initialize": init_result,
                "thread": thread,
            }
            if payload.include_events:
                output["events"] = events
            return output

        turn_req_id = next_id
        await send(
            ws,
            {
                "id": turn_req_id,
                "method": "turn/start",
                "params": {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": payload.prompt}],
                },
            },
        )
        turn_response = await wait_for_response(ws, turn_req_id)

        # Continue until turn/completed.
        while not turn_completed:
            msg = await recv(ws)
            await process_message(ws, msg)

    assistant_text = assistant_completed_text or "".join(assistant_deltas).strip()
    output = {
        "backend": "app_server_ws",
        "app_server_url": url,
        "thread_id": thread_id,
        "assistant_text": assistant_text,
        "turn": turn_response.get("turn") if isinstance(turn_response, dict) else None,
    }
    if payload.include_events:
        output["events"] = events
    return output


@app.get("/healthz")
async def healthz() -> dict:
    managed_running = bool(MANAGED_APP_SERVER_PROCESS and MANAGED_APP_SERVER_PROCESS.poll() is None)
    return {
        "status": "ok",
        "repo": str(REPO),
        "app_server_url": APP_SERVER_URL,
        "managed_app_server": {
            "started_by_gateway": MANAGED_APP_SERVER_STARTED_BY_GATEWAY,
            "running": managed_running,
            "pid": MANAGED_APP_SERVER_PROCESS.pid if managed_running and MANAGED_APP_SERVER_PROCESS else None,
            "listen_url": MANAGED_APP_SERVER_LISTEN_URL,
        },
    }


@app.get("/projects")
async def list_projects(limit: int = Query(default=200, ge=1, le=5000)) -> dict:
    sql = """
    SELECT
      cwd,
      COUNT(*) AS thread_count,
      MAX(updated_at) AS last_updated_at
    FROM threads
    WHERE archived = 0
      AND cwd IS NOT NULL
      AND cwd != ''
    GROUP BY cwd
    ORDER BY last_updated_at DESC
    LIMIT ?
    """
    state_db, rows = _query_state_db(sql, (limit,))
    data = []
    for row in rows:
        cwd = row["cwd"]
        data.append(
            {
                "project_id": cwd,
                "cwd": cwd,
                "name": Path(cwd).name or cwd,
                "thread_count": int(row["thread_count"] or 0),
                "last_updated_at": _to_iso(row["last_updated_at"]),
            }
        )
    return {"state_db": str(state_db), "data": data}


@app.get("/threads")
async def list_threads(
    cwd: str | None = Query(default=None, description="Exact project folder path"),
    limit: int = Query(default=100, ge=1, le=5000),
) -> dict:
    sql_base = """
    SELECT
      id,
      title,
      cwd,
      updated_at,
      created_at,
      model_provider,
      model,
      reasoning_effort,
      first_user_message
    FROM threads
    WHERE archived = 0
    """
    args: list = []
    if cwd:
        sql_base += " AND cwd = ?"
        args.append(cwd)
    sql_base += " ORDER BY updated_at DESC LIMIT ?"
    args.append(limit)
    state_db, rows = _query_state_db(sql_base, tuple(args))
    data = []
    for row in rows:
        title = row["title"] or ""
        first_msg = row["first_user_message"] or ""
        preview = _short_text(title if title else first_msg, 220)
        display_name = _thread_display_name(row["cwd"], title, first_msg, row["id"])
        data.append(
            {
                "thread_id": row["id"],
                "short_thread_id": str(row["id"])[:8],
                "project_name": Path(row["cwd"]).name if row["cwd"] else None,
                "title": title,
                "first_user_message": _short_text(first_msg, 220),
                "preview": preview,
                "display_name": display_name,
                "cwd": row["cwd"],
                "created_at": _to_iso(row["created_at"]),
                "updated_at": _to_iso(row["updated_at"]),
                "model_provider": row["model_provider"],
                "model": row["model"],
                "reasoning_effort": row["reasoning_effort"],
            }
        )
    return {"state_db": str(state_db), "data": data}


@app.get("/threads/{thread_id}")
async def get_thread(thread_id: str) -> dict:
    sql = """
    SELECT
      id,
      title,
      cwd,
      created_at,
      updated_at,
      model_provider,
      model,
      reasoning_effort,
      first_user_message
    FROM threads
    WHERE id = ?
    LIMIT 1
    """
    state_db, rows = _query_state_db(sql, (thread_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Thread not found")
    row = rows[0]
    title = row["title"] or ""
    first_msg = row["first_user_message"] or ""
    preview = _short_text(title if title else first_msg, 400)
    display_name = _thread_display_name(row["cwd"], title, first_msg, row["id"])
    return {
        "state_db": str(state_db),
        "thread": {
            "thread_id": row["id"],
            "short_thread_id": str(row["id"])[:8],
            "project_name": Path(row["cwd"]).name if row["cwd"] else None,
            "title": title,
            "first_user_message": _short_text(first_msg, 400),
            "preview": preview,
            "display_name": display_name,
            "cwd": row["cwd"],
            "created_at": _to_iso(row["created_at"]),
            "updated_at": _to_iso(row["updated_at"]),
            "model_provider": row["model_provider"],
            "model": row["model"],
            "reasoning_effort": row["reasoning_effort"],
        },
    }


@app.get("/debug/logs")
async def debug_logs(lines: int = Query(default=200, ge=1, le=5000)) -> dict:
    if not DEBUG_MODE:
        raise HTTPException(status_code=403, detail="debug mode is disabled")
    if not LOG_FILE:
        return {"status": "ok", "log_file": None, "lines": []}
    log_path = Path(LOG_FILE).expanduser()
    if not log_path.exists():
        return {"status": "ok", "log_file": str(log_path), "lines": []}
    return {
        "status": "ok",
        "log_file": str(log_path),
        "lines": _tail_file(log_path, lines),
    }


@app.get("/status")
async def get_status() -> dict:
    return _run_command(["git", "status", "--porcelain", "--branch"], REPO, timeout=30)


@app.get("/diff")
async def get_diff(max_lines: int = Query(default=200, ge=1, le=5000)) -> dict:
    result = _run_command(["git", "diff"], REPO, timeout=30)
    lines = result["stdout"].splitlines()
    result["diff"] = "\n".join(lines[:max_lines])
    result["truncated"] = len(lines) > max_lines
    del result["stdout"]
    return result


@app.get("/file")
async def get_file(path: str = Query(..., description="Path relative to repository root")) -> dict:
    target = _resolve_repo_path(path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"path": path, "content": content}


@app.get("/handoff")
async def get_handoff() -> dict:
    handoff_path = REPO / ".agent" / "handoff.md"
    if not handoff_path.exists():
        return {"handoff": ""}
    try:
        return {"handoff": handoff_path.read_text(encoding="utf-8")}
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/task")
async def post_task(payload: TaskRequest) -> dict:
    agent_dir = REPO / ".agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    task_path = agent_dir / "current_task.md"
    try:
        task_path.write_text(payload.text, encoding="utf-8")
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "ok"}


@app.post("/codex")
async def post_codex(payload: CodexRequest) -> dict:
    LOGGER.info(
        "post_codex backend=%s kind=%s cwd=%s include_events=%s max_events=%s",
        payload.backend,
        payload.kind,
        payload.cwd,
        payload.include_events,
        payload.max_events,
    )
    if payload.backend == "app_server_ws":
        # For remote app-server usage (including SSH tunnels), caller may not
        # know remote absolute paths in advance. In that case we start without
        # cwd override and return server-selected cwd in thread/start response.
        ws_cwd: Path | None = Path(payload.cwd).resolve() if payload.cwd else None
        # Run the WS client in a dedicated thread to avoid event-loop interaction
        # issues under ASGI runtimes.
        return await asyncio.to_thread(
            lambda: asyncio.run(_run_codex_via_app_server_ws(payload, ws_cwd))
        )
    cwd = Path(payload.cwd).resolve() if payload.cwd else REPO
    if not cwd.exists() or not cwd.is_dir():
        raise HTTPException(status_code=400, detail="cwd must be an existing directory")
    cmd = _build_codex_command(payload, cwd)
    return _run_command(cmd, cwd=cwd, timeout=CODEX_TIMEOUT_SECONDS)


def main() -> None:
    global REPO, CODEX_TIMEOUT_SECONDS, GATEWAY_API_KEY, GATEWAY_API_KEY_HEADER, AUTH_DISABLED, APP_SERVER_URL
    global DEBUG_MODE, LOG_LEVEL, LOG_FILE, LOG_REQUESTS, MAX_OUTPUT_CHARS

    parser = argparse.ArgumentParser(description="Codex Gateway (FastAPI)")
    parser.add_argument(
        "--repo",
        default=os.environ.get("REPO", os.getcwd()),
        help="Repository root (default: current directory)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8000")),
        help="Port to listen on",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="Host to bind to (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=CODEX_TIMEOUT_SECONDS,
        help="Timeout for Codex command execution",
    )
    parser.add_argument(
        "--max-output-chars",
        type=int,
        default=MAX_OUTPUT_CHARS,
        help="Maximum stdout/stderr characters returned per command result",
    )
    parser.add_argument(
        "--api-key-env",
        default=os.environ.get("GATEWAY_API_KEY_ENV", "CODEX_GATEWAY_API_KEY"),
        help="Environment variable name that stores Bearer API key",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Bearer API key value (overrides --api-key-env if set)",
    )
    parser.add_argument(
        "--api-key-header",
        default=os.environ.get("GATEWAY_API_KEY_HEADER", GATEWAY_API_KEY_HEADER),
        help="Alternative API key header name (default: x-api-key)",
    )
    parser.add_argument(
        "--disable-auth",
        action="store_true",
        help="Disable Bearer auth check even if API key is configured",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=DEBUG_MODE,
        help="Enable debug mode and verbose logging",
    )
    parser.add_argument(
        "--log-level",
        default=LOG_LEVEL,
        help="Logging level (DEBUG, INFO, WARNING, ERROR)",
    )
    parser.add_argument(
        "--log-file",
        default=LOG_FILE,
        help="Optional log file path; stdout logging is always enabled",
    )
    parser.add_argument(
        "--log-requests",
        action="store_true",
        default=LOG_REQUESTS,
        help="Log every HTTP request with status and duration",
    )
    parser.add_argument(
        "--spawn-app-server",
        action="store_true",
        default=os.environ.get("GATEWAY_SPAWN_APP_SERVER", "0") == "1",
        help="Start `codex app-server` as a subprocess and keep it alive while gateway runs",
    )
    parser.add_argument(
        "--spawn-app-server-listen",
        default=os.environ.get("GATEWAY_SPAWN_APP_SERVER_LISTEN", APP_SERVER_URL),
        help="Listen URL for managed app-server (default: ws://127.0.0.1:4500)",
    )
    parser.add_argument(
        "--codex-bin",
        default=os.environ.get("CODEX_BIN", "codex"),
        help="Path to codex executable for managed app-server mode",
    )

    args = parser.parse_args()
    REPO = Path(args.repo).resolve()
    CODEX_TIMEOUT_SECONDS = args.timeout_seconds
    MAX_OUTPUT_CHARS = args.max_output_chars
    APP_SERVER_URL = args.spawn_app_server_listen if args.spawn_app_server else APP_SERVER_URL
    AUTH_DISABLED = args.disable_auth or (os.environ.get("GATEWAY_DISABLE_AUTH", "0") == "1")
    DEBUG_MODE = args.debug
    LOG_LEVEL = str(args.log_level).upper()
    LOG_FILE = args.log_file.strip()
    LOG_REQUESTS = args.log_requests
    _configure_logging()
    LOGGER.info(
        "gateway startup repo=%s host=%s port=%s auth_disabled=%s app_server_url=%s",
        REPO,
        args.host,
        args.port,
        AUTH_DISABLED,
        APP_SERVER_URL,
    )
    if args.api_key is not None:
        GATEWAY_API_KEY = args.api_key.strip()
    else:
        GATEWAY_API_KEY = os.environ.get(args.api_key_env, "").strip()
    GATEWAY_API_KEY_HEADER = str(args.api_key_header).strip().lower()

    if args.spawn_app_server:
        _start_managed_app_server(args.codex_bin, args.spawn_app_server_listen)

    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        _stop_managed_app_server()


if __name__ == "__main__":
    main()
