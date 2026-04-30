#!/usr/bin/env python3
"""
FastAPI gateway for repository introspection and Codex CLI execution.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import itertools
import json
import logging
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Awaitable, Callable

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from cgw.auth import extract_api_key_from_request, token_fingerprint
from cgw.config import GatewayConfig
from cgw.jobs import format_job_view, is_significant_ws_event, job_now, parse_unified_diff, prune_jobs
from cgw.models import CodexJobApprovalRequest, CodexJobRequest, CodexRequest, TaskRequest
from cgw.state_db import query_state_db, thread_display_name
from cgw.text_utils import clip_text, short_text, tail_file


app = FastAPI(title="Codex Gateway", version="1.0.0")
LOGGER = logging.getLogger("codex_gateway")

# Mutable runtime config, initialized from env and overridden by CLI args.
_CONFIG = GatewayConfig.from_env()
REPO = _CONFIG.repo
CODEX_TIMEOUT_SECONDS = _CONFIG.codex_timeout_seconds
APP_SERVER_URL = _CONFIG.app_server_url
APP_SERVER_BEARER_TOKEN = _CONFIG.app_server_bearer_token
APP_SERVER_TIMEOUT_SECONDS = _CONFIG.app_server_timeout_seconds
GATEWAY_API_KEY = _CONFIG.gateway_api_key
GATEWAY_API_KEY_HEADER = _CONFIG.gateway_api_key_header
AUTH_DISABLED = _CONFIG.auth_disabled
DEBUG_MODE = _CONFIG.debug_mode
LOG_LEVEL = _CONFIG.log_level
LOG_FILE = _CONFIG.log_file
LOG_REQUESTS = _CONFIG.log_requests
MAX_OUTPUT_CHARS = _CONFIG.max_output_chars
MANAGED_APP_SERVER_PROCESS: subprocess.Popen[str] | None = None
MANAGED_APP_SERVER_LISTEN_URL: str | None = None
MANAGED_APP_SERVER_STARTED_BY_GATEWAY = False
JOB_POLL_AFTER_SECONDS = _CONFIG.job_poll_after_seconds
JOB_TTL_SECONDS = _CONFIG.job_ttl_seconds
JOB_MAX_ITEMS = _CONFIG.job_max_items
JOB_LONG_POLL_ENABLED = _CONFIG.job_long_poll_enabled
JOB_LONG_POLL_MAX_SECONDS = _CONFIG.job_long_poll_max_seconds
CODEX_SYNC_MAX_WAIT_SECONDS = _CONFIG.codex_sync_max_wait_seconds
JOB_DEBUG_TRACE_ENABLED = _CONFIG.job_debug_trace_enabled
JOB_DEBUG_TRACE_MAX_ITEMS = _CONFIG.job_debug_trace_max_items
APPROVAL_POLL_INTERVAL_SECONDS = _CONFIG.approval_poll_interval_seconds
JOBS: dict[str, dict] = {}
JOB_COUNTER = itertools.count(1)
PUBLIC_SCHEMA_ENABLED = _CONFIG.public_schema_enabled
SCHEMA_IMPORT_KEY = _CONFIG.schema_import_key
SCHEMA_IMPORT_KEY_PARAM = _CONFIG.schema_import_key_param


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
    if request.url.path == "/gpt-action-schema.json" and PUBLIC_SCHEMA_ENABLED:
        return await call_next(request)
    if request.url.path == "/gpt-action-schema.json" and SCHEMA_IMPORT_KEY and SCHEMA_IMPORT_KEY_PARAM:
        provided = request.query_params.get(SCHEMA_IMPORT_KEY_PARAM, "")
        if provided and hmac.compare_digest(provided, SCHEMA_IMPORT_KEY):
            return await call_next(request)
    # Authentication is enabled when CODEX_GATEWAY_API_KEY is set and
    # GATEWAY_DISABLE_AUTH is not set to 1.
    if GATEWAY_API_KEY and not AUTH_DISABLED:
        token = extract_api_key_from_request(request, GATEWAY_API_KEY_HEADER)
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
                token_fingerprint(token),
                token_fingerprint(GATEWAY_API_KEY),
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


def _collect_strings(obj) -> list[str]:
    out: list[str] = []
    if isinstance(obj, str):
        out.append(obj)
        return out
    if isinstance(obj, dict):
        for v in obj.values():
            out.extend(_collect_strings(v))
        return out
    if isinstance(obj, list):
        for v in obj:
            out.extend(_collect_strings(v))
    return out


def _collect_values_by_key(obj, target_key: str) -> list:
    out: list = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == target_key:
                out.append(v)
            out.extend(_collect_values_by_key(v, target_key))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_collect_values_by_key(v, target_key))
    return out


def _looks_like_diff(text: str) -> bool:
    if not text or len(text) < 8:
        return False
    return (
        "diff --git " in text
        or "@@ " in text
        or text.startswith("--- ")
        or text.startswith("+++ ")
    )


def _extract_best_diff_text(msg: dict) -> str | None:
    params = msg.get("params")
    strings = _collect_strings(params)
    # Prefer explicit diff-like keys first when present.
    for key in ("diff", "unified_diff", "patch"):
        vals = _collect_values_by_key(params, key)
        for v in vals:
            if isinstance(v, str) and _looks_like_diff(v):
                strings.append(v)
    diff_candidates = [s for s in strings if _looks_like_diff(s)]
    if not diff_candidates:
        return None
    # Prefer the largest candidate as it is usually the full patch.
    diff_candidates.sort(key=len, reverse=True)
    return diff_candidates[0]


def _register_diff_update(job: dict, method: str | None, diff_text: str) -> None:
    capped_diff = diff_text[:400000]
    mode = job.get("diff_mode", "live")
    now_iso = _to_iso(job_now())
    job["latest_diff_text"] = capped_diff

    if mode == "live":
        version = int(job.get("diff_live_version", 0)) + 1
        job["diff_live_version"] = version
        job["diff_live_available"] = True
        updates = job["diff_live_updates"]
        updates.append(
            {
                "version": version,
                "method": method,
                "updated_at": now_iso,
                "size": len(capped_diff),
                "diff": capped_diff,
            }
        )
        if len(updates) > 200:
            del updates[: len(updates) - 200]
        job["diff_hint"] = "live update available"
    elif mode == "final_only":
        # Capture latest patch, but do not publish incremental updates.
        job["diff_hint"] = "diff captured (final only)"
    else:
        # off mode: keep internal trace for potential diagnostics.
        job["diff_hint"] = "diff capture disabled"


def _extract_approval_summary(method: str, params: dict) -> dict:
    request_type = "unknown"
    if "commandExecution" in method:
        request_type = "command_execution"
    elif "fileChange" in method:
        request_type = "file_change"
    elif "permissions" in method:
        request_type = "permissions"

    strings = _collect_strings(params)
    summary = short_text(" | ".join(strings), 220) if strings else method
    return {
        "request_type": request_type,
        "summary": summary,
    }


def _approval_policy_key(approval_request: dict) -> str:
    request_type = approval_request.get("request_type", "unknown")
    summary = approval_request.get("summary", "")
    return f"{request_type}:{summary[:120]}"


def _append_job_debug_trace(job: dict, msg: dict) -> None:
    if not JOB_DEBUG_TRACE_ENABLED:
        return
    trace = job.get("debug_trace")
    if trace is None:
        return
    method = msg.get("method")
    params = msg.get("params")
    row = {
        "ts": _to_iso(job_now()),
        "method": method if isinstance(method, str) else None,
        "param_keys": sorted(list(params.keys())) if isinstance(params, dict) else [],
        "diff_like_keys_present": {
            "diff": bool(_collect_values_by_key(params, "diff")) if params is not None else False,
            "unified_diff": bool(_collect_values_by_key(params, "unified_diff")) if params is not None else False,
            "patch": bool(_collect_values_by_key(params, "patch")) if params is not None else False,
            "changes": bool(_collect_values_by_key(params, "changes")) if params is not None else False,
        },
    }
    trace.append(row)
    if len(trace) > JOB_DEBUG_TRACE_MAX_ITEMS:
        del trace[: len(trace) - JOB_DEBUG_TRACE_MAX_ITEMS]


def _resolve_repo_path(path: str) -> Path:
    target = (REPO / path).resolve()
    try:
        target.relative_to(REPO)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Illegal path") from exc
    return target


def _validate_app_server_cwd(cwd_value: str | None) -> Path | None:
    if not cwd_value:
        return None
    cwd = Path(cwd_value).expanduser().resolve()
    if not cwd.exists():
        raise HTTPException(status_code=400, detail=f"cwd does not exist: {cwd}")
    if not cwd.is_dir():
        raise HTTPException(status_code=400, detail=f"cwd is not a directory: {cwd}")
    return cwd


def _gateway_base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _render_gpt_action_schema(base_url: str) -> dict:
    template_path = Path(__file__).resolve().parent / "gpt_action_schema.template.json"
    template: dict
    if template_path.exists():
        try:
            template = json.loads(template_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Invalid schema template: {exc}") from exc
    else:
        # Installed tool mode fallback: template file may not be present
        # next to the module, so use bundled JSON from a Python module.
        try:
            from gpt_action_schema_template_data import GPT_ACTION_SCHEMA_TEMPLATE_JSON

            template = json.loads(GPT_ACTION_SCHEMA_TEMPLATE_JSON)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"Schema template not found: {exc}") from exc

    for server in template.get("servers", []):
        if server.get("url") == "https://<GATEWAY_HOST>":
            server["url"] = base_url
    return template


def _schema_hash(schema: dict) -> str:
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _render_gpt_system_instruction() -> str:
    template_path = Path(__file__).resolve().parent / "gpt_system_instruction.template.txt"
    if template_path.exists():
        try:
            return template_path.read_text(encoding="utf-8")
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Invalid system instruction template: {exc}") from exc
    try:
        from gpt_system_instruction_template_data import GPT_SYSTEM_INSTRUCTION_TEMPLATE_TEXT

        return GPT_SYSTEM_INSTRUCTION_TEMPLATE_TEXT
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"System instruction template not found: {exc}") from exc


def _schema_import_urls(port: int) -> tuple[str, str | None]:
    base = f"http://127.0.0.1:{port}"
    plain = f"{base}/gpt-action-schema.json"
    if SCHEMA_IMPORT_KEY and SCHEMA_IMPORT_KEY_PARAM:
        secured = f"{plain}?{SCHEMA_IMPORT_KEY_PARAM}={SCHEMA_IMPORT_KEY}"
        return secured, f"{plain}?{SCHEMA_IMPORT_KEY_PARAM}=<value>"
    return plain, None


def _to_iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC).isoformat()
    except Exception:
        return None


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

    stdout, stdout_truncated = clip_text(result.stdout, MAX_OUTPUT_CHARS)
    stderr, stderr_truncated = clip_text(result.stderr, MAX_OUTPUT_CHARS)
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


async def _run_codex_via_app_server_ws(
    payload: CodexRequest,
    cwd: Path | None,
    on_update: Callable[[dict], None] | None = None,
    on_server_request: Callable[[dict], Awaitable[dict]] | None = None,
) -> dict:
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
        if on_update:
            try:
                on_update(msg)
            except Exception:
                LOGGER.debug("on_update callback failed", exc_info=True)

        # Handle server-initiated request (for approvals/user input).
        if "id" in msg and "method" in msg and "result" not in msg and "error" not in msg:
            request_id = msg["id"]
            request_method = msg["method"]
            if request_method in (
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            ):
                if on_server_request:
                    decision_result = await on_server_request(
                        {
                            "id": request_id,
                            "method": request_method,
                            "params": msg.get("params", {}),
                        }
                    )
                    await send(ws, {"id": request_id, "result": decision_result})
                else:
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
            if payload.reasoning_effort:
                params["modelReasoningEffort"] = payload.reasoning_effort
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


@app.get("/gpt-action-schema.json")
async def gpt_action_schema(request: Request) -> dict:
    schema = _render_gpt_action_schema(_gateway_base_url(request))
    schema_hash = _schema_hash(schema)
    schema["x-schema-hash"] = schema_hash
    return JSONResponse(
        content=schema,
        headers={
            "ETag": schema_hash,
            "X-Schema-Sha256": schema_hash,
        },
    )


@app.get("/gpt-system-instruction.txt")
async def gpt_system_instruction() -> dict:
    return {"instruction": _render_gpt_system_instruction()}


@app.get("/projects")
async def list_projects(
    limit: int = Query(default=200, ge=1, le=5000),
    existing_only: bool = Query(default=True, description="Return only projects with existing cwd"),
) -> dict:
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
    state_db, rows = query_state_db(sql, (limit,))
    data = []
    for row in rows:
        cwd = row["cwd"]
        if existing_only and (not cwd or not Path(cwd).exists() or not Path(cwd).is_dir()):
            continue
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
    existing_only: bool = Query(default=True, description="Return only threads with existing cwd"),
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
    state_db, rows = query_state_db(sql_base, tuple(args))
    data = []
    for row in rows:
        thread_cwd = row["cwd"]
        if existing_only and (not thread_cwd or not Path(thread_cwd).exists() or not Path(thread_cwd).is_dir()):
            continue
        title = row["title"] or ""
        first_msg = row["first_user_message"] or ""
        preview = short_text(title if title else first_msg, 220)
        display_name = thread_display_name(thread_cwd, title, first_msg, row["id"])
        data.append(
            {
                "thread_id": row["id"],
                "short_thread_id": str(row["id"])[:8],
                "project_name": Path(thread_cwd).name if thread_cwd else None,
                "title": title,
                "first_user_message": short_text(first_msg, 220),
                "preview": preview,
                "display_name": display_name,
                "cwd": thread_cwd,
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
    state_db, rows = query_state_db(sql, (thread_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Thread not found")
    row = rows[0]
    title = row["title"] or ""
    first_msg = row["first_user_message"] or ""
    preview = short_text(title if title else first_msg, 400)
    display_name = thread_display_name(row["cwd"], title, first_msg, row["id"])
    return {
        "state_db": str(state_db),
        "thread": {
            "thread_id": row["id"],
            "short_thread_id": str(row["id"])[:8],
            "project_name": Path(row["cwd"]).name if row["cwd"] else None,
            "title": title,
            "first_user_message": short_text(first_msg, 400),
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
        "lines": tail_file(log_path, lines),
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
    # Fail fast for invalid cwd in app_server_ws mode.
    if payload.backend == "app_server_ws":
        _validate_app_server_cwd(payload.cwd)
    # Keep sync API for compatibility, but avoid proxy timeouts:
    # run as background job and wait a short window for completion.
    if payload.backend == "app_server_ws":
        job = _create_job(payload)
        job_id = job["job_id"]
        try:
            await asyncio.wait_for(job["done_event"].wait(), timeout=CODEX_SYNC_MAX_WAIT_SECONDS)
        except TimeoutError:
            return {
                "status": "in_progress",
                "message": "Codex is still working. Continue polling job status.",
                "job_id": job_id,
                "job": format_job_view(job, poll_after_seconds=JOB_POLL_AFTER_SECONDS, to_iso=_to_iso, include_result=False),
            }
        # Completed within wait window: return final result/error in sync response.
        if job["status"] == "completed":
            return job["result"]
        if job["status"] == "failed":
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "Codex job failed",
                    "job_id": job_id,
                    "job": format_job_view(job, poll_after_seconds=JOB_POLL_AFTER_SECONDS, to_iso=_to_iso, include_result=False),
                },
            )
    return await _execute_codex(payload)


async def _execute_codex(payload: CodexRequest) -> dict:
    return await _execute_codex_with_updates(payload, on_update=None, on_server_request=None)


async def _execute_codex_with_updates(
    payload: CodexRequest,
    on_update: Callable[[dict], None] | None,
    on_server_request: Callable[[dict], Awaitable[dict]] | None,
) -> dict:
    if payload.backend == "app_server_ws":
        # For remote app-server usage (including SSH tunnels), caller may not
        # know remote absolute paths in advance. In that case we start without
        # cwd override and return server-selected cwd in thread/start response.
        ws_cwd = _validate_app_server_cwd(payload.cwd)
        # Run the WS client in a dedicated thread to avoid event-loop interaction
        # issues under ASGI runtimes.
        return await asyncio.to_thread(
            lambda: asyncio.run(
                _run_codex_via_app_server_ws(
                    payload, ws_cwd, on_update=on_update, on_server_request=on_server_request
                )
            )
        )
    cwd = Path(payload.cwd).resolve() if payload.cwd else REPO
    if not cwd.exists() or not cwd.is_dir():
        raise HTTPException(status_code=400, detail="cwd must be an existing directory")
    cmd = _build_codex_command(payload, cwd)
    return _run_command(cmd, cwd=cwd, timeout=CODEX_TIMEOUT_SECONDS)


async def _run_job(job_id: str) -> None:
    job = JOBS[job_id]
    loop = asyncio.get_running_loop()
    notify_event: asyncio.Event = job["notify_event"]

    def _notify_job_update(method: str | None = None, wake_poll: bool = False) -> None:
        now = job_now()
        job["updated_at"] = now
        if method:
            job["last_event_method"] = method
        if not wake_poll:
            return
        if notify_event.is_set():
            return
        loop.call_soon_threadsafe(notify_event.set)

    def _on_update(msg: dict) -> None:
        _append_job_debug_trace(job, msg)
        method = msg.get("method")
        diff_text = _extract_best_diff_text(msg)
        if diff_text:
            _register_diff_update(job, method if isinstance(method, str) else None, diff_text)
        if method == "item/agentMessage/delta":
            delta = msg.get("params", {}).get("delta")
            if isinstance(delta, str):
                prev = job.get("last_update_text") or ""
                # Keep only a short rolling preview.
                merged = (prev + delta)[-1000:]
                job["last_update_text"] = merged
        elif method == "item/completed":
            item = msg.get("params", {}).get("item", {})
            if item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    job["last_update_text"] = text[-1000:]
        m = method if isinstance(method, str) else None
        _notify_job_update(m, wake_poll=is_significant_ws_event(m, msg))

    async def _on_server_request(req: dict) -> dict:
        request_id = req["id"]
        method = str(req.get("method", ""))
        params = req.get("params", {}) if isinstance(req.get("params"), dict) else {}
        meta = _extract_approval_summary(method, params)
        approval_request = {
            "request_id": request_id,
            "method": method,
            "request_type": meta["request_type"],
            "summary": meta["summary"],
            "created_at": _to_iso(job_now()),
            "params": params,
            "options": ["approve_once", "approve_all_similar", "deny", "guidance"],
        }
        policy_key = _approval_policy_key(approval_request)

        # Auto-apply previously approved policy.
        for policy in job.get("approval_policies", []):
            if policy.get("key") == policy_key:
                job.setdefault("approval_history", []).append(
                    {
                        "request_id": request_id,
                        "decision": "approve_all_similar(auto)",
                        "at": _to_iso(job_now()),
                    }
                )
                _notify_job_update("job/approval_auto_applied", wake_poll=True)
                return {"decision": "approve"}

        job["approval_required"] = True
        job["approval_request"] = approval_request
        job["status"] = "waiting_approval"
        _notify_job_update("job/waiting_approval", wake_poll=True)

        while True:
            decisions = job.get("approval_decisions", {})
            if request_id in decisions:
                decision = decisions.pop(request_id)
                break
            await asyncio.sleep(APPROVAL_POLL_INTERVAL_SECONDS)

        job["approval_required"] = False
        job["approval_request"] = None
        job["status"] = "running"
        job.setdefault("approval_history", []).append(
            {
                "request_id": request_id,
                "decision": decision.get("decision"),
                "at": _to_iso(job_now()),
            }
        )
        if decision.get("decision") == "approve_all_similar":
            job.setdefault("approval_policies", []).append(
                {
                    "key": policy_key,
                    "source_request_id": request_id,
                    "created_at": _to_iso(job_now()),
                    "scope_hint": decision.get("scope_hint"),
                }
            )

        if decision.get("decision") == "guidance":
            guidance_text = (decision.get("guidance_text") or "").strip()
            if guidance_text:
                job["last_update_text"] = short_text(
                    f"Operator guidance received: {guidance_text}", 1000
                )
            _notify_job_update("job/approval_guidance", wake_poll=True)
            return {"decision": "decline"}

        if decision.get("decision") == "approve_once":
            _notify_job_update("job/approval_approved", wake_poll=True)
            return {"decision": "approve"}
        if decision.get("decision") == "approve_all_similar":
            _notify_job_update("job/approval_approved_all_similar", wake_poll=True)
            return {"decision": "approve"}

        _notify_job_update("job/approval_denied", wake_poll=True)
        return {"decision": "decline"}

    job["status"] = "running"
    job["started_at"] = job_now()
    job["updated_at"] = job["started_at"]
    _notify_job_update("job/running", wake_poll=True)
    try:
        result = await _execute_codex_with_updates(
            job["payload"], on_update=_on_update, on_server_request=_on_server_request
        )
        job["result"] = result
        if isinstance(result, dict):
            job["thread_id"] = result.get("thread_id")
        job["status"] = "completed"
        if job.get("diff_mode") in {"live", "final_only"} and job.get("latest_diff_text"):
            job["diff_final_available"] = True
            job["final_diff_text"] = job.get("latest_diff_text")
            job["diff_hint"] = "final diff ready"
    except HTTPException as exc:
        job["status"] = "failed"
        job["error"] = {"status_code": exc.status_code, "detail": exc.detail}
    except Exception as exc:  # pragma: no cover
        LOGGER.exception("job execution failed job_id=%s", job_id)
        job["status"] = "failed"
        job["error"] = {"status_code": 500, "detail": str(exc)}
    finally:
        ts = job_now()
        if job["status"] == "failed" and job.get("latest_diff_text"):
            job["diagnostic_diff_available"] = True
            job["diff_hint"] = "diagnostic diff available"
        job["updated_at"] = ts
        job["completed_at"] = ts
        job["next_poll_after"] = ts
        job["last_event_method"] = "job/completed" if job["status"] == "completed" else "job/failed"
        _notify_job_update(job["last_event_method"], wake_poll=True)
        done_event: asyncio.Event = job["done_event"]
        if not done_event.is_set():
            done_event.set()
        prune_jobs(JOBS, ttl_seconds=JOB_TTL_SECONDS, max_items=JOB_MAX_ITEMS)


def _create_job(payload: CodexRequest) -> dict:
    prune_jobs(JOBS, ttl_seconds=JOB_TTL_SECONDS, max_items=JOB_MAX_ITEMS)
    job_id = f"job_{job_now()}_{next(JOB_COUNTER)}"
    ts = job_now()
    job = {
        "job_id": job_id,
        "status": "queued",
        "created_at": ts,
        "started_at": None,
        "updated_at": ts,
        "completed_at": None,
        "thread_id": None,
        "next_poll_after": ts,
        "last_event_method": "job/queued",
        "last_update_text": None,
        "approval_required": False,
        "approval_request": None,
        "approval_policies": [],
        "approval_history": [],
        "approval_decisions": {},
        "diff_mode": payload.diff_mode,
        "diff_live_available": False,
        "diff_live_version": 0,
        "diff_final_available": False,
        "diff_hint": None,
        "diagnostic_diff_available": False,
        "latest_diff_text": None,
        "final_diff_text": None,
        "diff_live_updates": [],
        "notify_event": asyncio.Event(),
        "done_event": asyncio.Event(),
        "error": None,
        "result": None,
        "debug_trace": [],
        "payload": payload,
    }
    JOBS[job_id] = job
    asyncio.create_task(_run_job(job_id))
    return job


@app.post("/codex/jobs")
async def create_codex_job(request: CodexJobRequest) -> dict:
    job = _create_job(request.payload)
    return format_job_view(job, poll_after_seconds=JOB_POLL_AFTER_SECONDS, to_iso=_to_iso, include_result=False)


@app.post("/codex/jobs/{job_id}/approval")
async def post_codex_job_approval(job_id: str, payload: CodexJobApprovalRequest) -> dict:
    prune_jobs(JOBS, ttl_seconds=JOB_TTL_SECONDS, max_items=JOB_MAX_ITEMS)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.get("approval_required") or not job.get("approval_request"):
        raise HTTPException(status_code=409, detail="No pending approval for this job")

    pending = job["approval_request"]
    if str(pending.get("request_id")) != str(payload.request_id):
        raise HTTPException(status_code=409, detail="request_id does not match current pending approval")

    if payload.decision == "guidance" and not (payload.guidance_text or "").strip():
        raise HTTPException(status_code=400, detail="guidance_text is required for guidance decision")

    job.setdefault("approval_decisions", {})[pending["request_id"]] = {
        "decision": payload.decision,
        "guidance_text": payload.guidance_text,
        "scope_hint": payload.scope_hint,
    }
    return {
        "status": "ok",
        "job": format_job_view(job, poll_after_seconds=JOB_POLL_AFTER_SECONDS, to_iso=_to_iso, include_result=False),
    }


@app.get("/codex/jobs/{job_id}")
async def get_codex_job(job_id: str) -> dict:
    prune_jobs(JOBS, ttl_seconds=JOB_TTL_SECONDS, max_items=JOB_MAX_ITEMS)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    now = job_now()
    if job["status"] in {"queued", "running"}:
        next_poll_after = int(job.get("next_poll_after", 0) or 0)
        if JOB_LONG_POLL_ENABLED and next_poll_after > now:
            wait_seconds = min(next_poll_after - now, JOB_LONG_POLL_MAX_SECONDS)
            if wait_seconds > 0:
                notify_event: asyncio.Event | None = job.get("notify_event")
                if notify_event:
                    try:
                        await asyncio.wait_for(notify_event.wait(), timeout=wait_seconds)
                    except TimeoutError:
                        pass
                    finally:
                        notify_event.clear()
                else:
                    await asyncio.sleep(wait_seconds)
                prune_jobs(JOBS, ttl_seconds=JOB_TTL_SECONDS, max_items=JOB_MAX_ITEMS)
                refreshed = JOBS.get(job_id)
                if not refreshed:
                    raise HTTPException(status_code=404, detail="Job not found")
                job = refreshed
                now = job_now()
                next_poll_after = int(job.get("next_poll_after", 0) or 0)
        if next_poll_after <= now:
            job["next_poll_after"] = now + JOB_POLL_AFTER_SECONDS
            job["updated_at"] = now
    return format_job_view(job, poll_after_seconds=JOB_POLL_AFTER_SECONDS, to_iso=_to_iso, include_result=False)


@app.get("/codex/jobs/{job_id}/result")
async def get_codex_job_result(job_id: str) -> dict:
    prune_jobs(JOBS, ttl_seconds=JOB_TTL_SECONDS, max_items=JOB_MAX_ITEMS)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] in {"queued", "running"}:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Job is not finished yet",
                "job": format_job_view(job, poll_after_seconds=JOB_POLL_AFTER_SECONDS, to_iso=_to_iso, include_result=False),
            },
        )
    if job["status"] == "failed":
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Job failed",
                "job": format_job_view(job, poll_after_seconds=JOB_POLL_AFTER_SECONDS, to_iso=_to_iso, include_result=False),
            },
        )
    return format_job_view(job, poll_after_seconds=JOB_POLL_AFTER_SECONDS, to_iso=_to_iso, include_result=True)


@app.get("/codex/jobs/{job_id}/diff/live")
async def get_codex_job_diff_live(
    job_id: str,
    since_version: int = Query(default=0, ge=0),
    max_chars: int = Query(default=200000, ge=1000, le=2000000),
) -> dict:
    prune_jobs(JOBS, ttl_seconds=JOB_TTL_SECONDS, max_items=JOB_MAX_ITEMS)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.get("diff_mode") != "live":
        return {
            "job_id": job_id,
            "status": job.get("status"),
            "diff_mode": job.get("diff_mode"),
            "diff_live_available": False,
            "diff_live_version": int(job.get("diff_live_version", 0)),
            "updates": [],
        }

    current_version = int(job.get("diff_live_version", 0))
    updates = []
    for upd in job.get("diff_live_updates", []):
        version = int(upd.get("version", 0))
        if version <= since_version:
            continue
        row = dict(upd)
        if isinstance(row.get("diff"), str):
            row["diff"] = row["diff"][:max_chars]
        updates.append(row)

    return {
        "job_id": job_id,
        "status": job.get("status"),
        "diff_mode": job.get("diff_mode"),
        "diff_live_available": bool(job.get("diff_live_available", False)),
        "diff_live_version": current_version,
        "updates": updates,
    }


@app.get("/codex/jobs/{job_id}/diff/final")
async def get_codex_job_diff_final(
    job_id: str,
    view: str = Query(default="raw", pattern="^(raw|split)$"),
    max_chars: int = Query(default=200000, ge=1000, le=2000000),
) -> dict:
    prune_jobs(JOBS, ttl_seconds=JOB_TTL_SECONDS, max_items=JOB_MAX_ITEMS)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    final_ready = bool(job.get("diff_final_available", False))
    diagnostic_ready = bool(job.get("diagnostic_diff_available", False))
    if not final_ready and not diagnostic_ready:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Final diff is not ready",
                "job": format_job_view(job, poll_after_seconds=JOB_POLL_AFTER_SECONDS, to_iso=_to_iso, include_result=False),
            },
        )

    raw = (job.get("final_diff_text") or job.get("latest_diff_text") or "")[:max_chars]
    if not raw:
        return {
            "job_id": job_id,
            "status": job.get("status"),
            "has_diff": False,
            "mode": "diagnostic" if diagnostic_ready and not final_ready else "final",
        }

    if view == "split":
        return {
            "job_id": job_id,
            "status": job.get("status"),
            "has_diff": True,
            "view": "split",
            "mode": "diagnostic" if diagnostic_ready and not final_ready else "final",
            "files": parse_unified_diff(raw),
        }

    return {
        "job_id": job_id,
        "status": job.get("status"),
        "has_diff": True,
        "view": "raw",
        "mode": "diagnostic" if diagnostic_ready and not final_ready else "final",
        "diff": raw,
    }


@app.get("/codex/jobs/{job_id}/diff")
async def get_codex_job_diff_compat(
    job_id: str,
    view: str = Query(default="raw", pattern="^(raw|split)$"),
    max_chars: int = Query(default=200000, ge=1000, le=2000000),
) -> dict:
    # Backward-compatible alias: returns final diff when available.
    return await get_codex_job_diff_final(job_id=job_id, view=view, max_chars=max_chars)


@app.get("/codex/jobs/{job_id}/debug-trace")
async def get_codex_job_debug_trace(
    job_id: str,
    limit: int = Query(default=200, ge=1, le=2000),
) -> dict:
    prune_jobs(JOBS, ttl_seconds=JOB_TTL_SECONDS, max_items=JOB_MAX_ITEMS)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    trace = job.get("debug_trace", [])
    return {
        "job_id": job_id,
        "status": job.get("status"),
        "trace_count": len(trace),
        "trace": trace[-limit:],
    }


def main() -> None:
    global REPO, CODEX_TIMEOUT_SECONDS, GATEWAY_API_KEY, GATEWAY_API_KEY_HEADER, AUTH_DISABLED, APP_SERVER_URL
    global DEBUG_MODE, LOG_LEVEL, LOG_FILE, LOG_REQUESTS, MAX_OUTPUT_CHARS
    global JOB_POLL_AFTER_SECONDS, JOB_TTL_SECONDS, JOB_MAX_ITEMS, JOB_LONG_POLL_ENABLED, JOB_LONG_POLL_MAX_SECONDS
    global CODEX_SYNC_MAX_WAIT_SECONDS

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
        "--job-poll-after-seconds",
        type=int,
        default=JOB_POLL_AFTER_SECONDS,
        help="Recommended polling interval for async job status",
    )
    parser.add_argument(
        "--job-ttl-seconds",
        type=int,
        default=JOB_TTL_SECONDS,
        help="Retention time for completed/failed async jobs in memory",
    )
    parser.add_argument(
        "--job-max-items",
        type=int,
        default=JOB_MAX_ITEMS,
        help="Maximum number of async jobs kept in memory",
    )
    parser.add_argument(
        "--job-long-poll-enabled",
        action=argparse.BooleanOptionalAction,
        default=JOB_LONG_POLL_ENABLED,
        help="Enable server-side long-poll behavior for async job status endpoint",
    )
    parser.add_argument(
        "--job-long-poll-max-seconds",
        type=int,
        default=JOB_LONG_POLL_MAX_SECONDS,
        help="Maximum hold time for one long-poll status request",
    )
    parser.add_argument(
        "--codex-sync-max-wait-seconds",
        type=int,
        default=CODEX_SYNC_MAX_WAIT_SECONDS,
        help="Max time for /codex sync request before returning in_progress with job_id",
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
    JOB_POLL_AFTER_SECONDS = args.job_poll_after_seconds
    JOB_TTL_SECONDS = args.job_ttl_seconds
    JOB_MAX_ITEMS = args.job_max_items
    JOB_LONG_POLL_ENABLED = args.job_long_poll_enabled
    JOB_LONG_POLL_MAX_SECONDS = args.job_long_poll_max_seconds
    CODEX_SYNC_MAX_WAIT_SECONDS = args.codex_sync_max_wait_seconds
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
    schema_import_url, schema_import_template = _schema_import_urls(args.port)
    LOGGER.info("gpt-action schema import url=%s", schema_import_url)
    if schema_import_template:
        LOGGER.info("gpt-action schema import url template=%s", schema_import_template)
    LOGGER.info("gpt-system instruction endpoint=http://127.0.0.1:%s/gpt-system-instruction.txt", args.port)
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
