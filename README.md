# Codex Gateway

This repository contains a FastAPI gateway for Codex automation.

## UV Project

This project is managed with `uv` and uses `pyproject.toml` for dependencies and command entry points.

### Install dependencies

```bash
uv sync
```

### Run tests

```bash
uv run --extra dev pytest -q
```

### Run locally from the project

```bash
uv run codex-gateway --repo /abs/path/to/repo --host 0.0.0.0 --port 8000
```

### Install as a tool

```bash
uv tool install .
```

After installation, the CLI entry point is:

```bash
codex-gateway
```

The project exposes the `codex-gateway` script through `[project.scripts]` in `pyproject.toml`.

## Environment Variables

### Core runtime

- `REPO`
  - Purpose: repository root used by gateway file and git endpoints.
  - Default: current process working directory.

- `HOST`
  - Purpose: HTTP bind host.
  - Default: `0.0.0.0`.

- `PORT`
  - Purpose: HTTP bind port.
  - Default: `8000`.

- `CODEX_TIMEOUT_SECONDS`
  - Purpose: timeout for CLI command execution (`backend=exec`).
  - Default: `120`.

- `model` (request payload field)
  - Purpose: overrides model for a single `runCodexTask` call.

- `reasoning_effort` (request payload field)
  - Purpose: overrides reasoning depth for a single `runCodexTask` call.
  - Supported values: `low`, `medium`, `high`, `xhigh`.

- `GATEWAY_MAX_OUTPUT_CHARS`
  - Purpose: maximum returned size for `stdout` and `stderr` in command results.
  - Default: `60000`.

- `GATEWAY_JOB_POLL_AFTER_SECONDS`
  - Purpose: recommended polling interval returned by async job endpoints.
  - Default: `15`.

- `GATEWAY_JOB_FIRST_POLL_GRACE_SECONDS`
  - Purpose: allows one early poll shortly after job creation.
  - Default: `3`.

- `GATEWAY_JOB_TTL_SECONDS`
  - Purpose: retention window for completed/failed async jobs in memory.
  - Default: `7200`.

- `GATEWAY_JOB_MAX_ITEMS`
  - Purpose: max number of async jobs stored in memory before old entries are pruned.
  - Default: `500`.

- `GATEWAY_JOB_LONG_POLL_ENABLED`
  - Purpose: enables server-side long polling for job status endpoint.
  - Values: `1` enable, `0` disable.
  - Default: `1`.

- `GATEWAY_JOB_LONG_POLL_MAX_SECONDS`
  - Purpose: max hold time for a single status request when polled too early.
  - Default: `20`.

- `GATEWAY_CODEX_SYNC_MAX_WAIT_SECONDS`
  - Purpose: max wait for `POST /codex` before returning `in_progress` with `job_id`.
  - Default: `20`.

- `GATEWAY_JOB_DEBUG_TRACE_ENABLED`
  - Purpose: capture compact WS event trace per job for diff parser diagnostics.
  - Values: `1` enable, `0` disable.
  - Default: `1`.

- `GATEWAY_JOB_DEBUG_TRACE_MAX_ITEMS`
  - Purpose: max number of debug trace rows stored per job.
  - Default: `400`.

### App Server backend

- `APP_SERVER_URL`
  - Purpose: default WebSocket URL for `backend=app_server_ws`.
  - Default: `ws://127.0.0.1:4500`.

- `APP_SERVER_BEARER_TOKEN`
  - Purpose: optional bearer token passed when connecting to app-server.
  - Default: unset.

- `APP_SERVER_TIMEOUT_SECONDS`
  - Purpose: timeout for waiting app-server messages.
  - Default: `180`.

### Gateway authentication

- `CODEX_GATEWAY_API_KEY`
  - Purpose: inbound bearer token for gateway HTTP auth.
  - Usage: clients must send `Authorization: Bearer <token>`.
  - Default: empty (auth inactive unless key is set).

- `GATEWAY_API_KEY_ENV`
  - Purpose: env var name used by CLI flag `--api-key-env`.
  - Default: `CODEX_GATEWAY_API_KEY`.

- `GATEWAY_API_KEY_HEADER`
  - Purpose: alternative header name for API key auth.
  - Default: `x-api-key`.
  - Note: gateway accepts both `Authorization: Bearer <token>` and this header.

- `GATEWAY_DISABLE_AUTH`
  - Purpose: disable auth check even if API key is configured.
  - Values: `1` disables auth, any other value keeps auth behavior.
  - Default: `0`.

### Managed app-server mode

- `GATEWAY_SPAWN_APP_SERVER`
  - Purpose: auto-start `codex app-server` as a subprocess.
  - Values: `1` to enable.
  - Default: `0`.

- `GATEWAY_SPAWN_APP_SERVER_LISTEN`
  - Purpose: listen URL used for managed app-server process.
  - Default: inherited from `APP_SERVER_URL`.

- `CODEX_BIN`
  - Purpose: Codex executable path used by managed app-server mode.
  - Default: `codex`.

### Protocol schema registry

- `GATEWAY_PROTOCOL_SCHEMA_CODEX_BIN`
  - Purpose: Codex binary used for protocol schema generation.
  - Default: `codex`.

### Logging and debug

- `GATEWAY_DEBUG`
  - Purpose: enable debug mode.
  - Values: `1` to enable.
  - Default: `0`.

- `GATEWAY_LOG_LEVEL`
  - Purpose: log severity level.
  - Values: `DEBUG`, `INFO`, `WARNING`, `ERROR`.
  - Default: `INFO`.

- `GATEWAY_LOG_FILE`
  - Purpose: optional log file path.
  - Default: unset (stdout logging only).

- `GATEWAY_LOG_REQUESTS`
  - Purpose: request logging toggle (method, path, status, duration).
  - Values: `1` to enable.
  - Default: `1`.

Structured lifecycle logs:
- Protocol registry access:
  - `event=protocol.list`
  - `event=protocol.get`
- Approval lifecycle:
  - `event=approval.request`
  - `event=approval.decision`
  - `event=approval.applied`

## GPT Action Assets

- `gpt_action_schema.template.json`: OpenAPI schema template for Custom GPT Actions.
- `gpt_system_instruction.template.txt`: system instruction template for the Custom GPT.
- `GET /gpt-system-instruction.txt`: returns current system instruction text used for GPT setup/testing.

The imported GPT OpenAPI is intentionally minimal (continuity-oriented):
- `getGatewayHealth`
- `createCodexJob`
- `getCodexJob`
- `postCodexJobApproval`
- `getCodexJobResult`
- `getProtocolSchemas`
- `getProtocolSchemaById`

### Import from URL

Gateway exposes a ready-to-import schema endpoint:

- `GET /gpt-action-schema.json`

Behavior:
- Returns JSON schema based on `gpt_action_schema.template.json`.
- Replaces `https://<GATEWAY_HOST>` with the current request host/protocol (supports reverse proxy headers).
- Returns schema revision hash in body field `x-schema-hash` and headers `ETag` / `X-Schema-Sha256`.
- Intended for ChatGPT "Import from URL" flow.
- Endpoint is protected by gateway auth by default.
- Set `GATEWAY_PUBLIC_SCHEMA=1` only if you explicitly want unauthenticated schema access.
- Optional import-only query key:
  - `GATEWAY_SCHEMA_IMPORT_KEY=<value>`
  - `GATEWAY_SCHEMA_IMPORT_KEY_PARAM=<param_name>` (default: `schema_key`)
  - Example:
    - `https://cdx.avfserv.net/gpt-action-schema.json?schema_key=<value>`

Startup logs also print ready-to-use URLs for:
- action schema import
- system instruction endpoint

Critical limitation:
- Keep the Custom GPT chat in the base Chats area in ChatGPT.
- Moving this Custom GPT conversation into Projects can break Actions execution for this integration.
- If Actions suddenly stop working after moving the chat, move back to a regular chat and re-import/re-open the action configuration.

## Protocol Schema Registry

The gateway exposes an in-memory protocol schema registry:

- `GET /protocol/schemas`
  - Returns full schema index (`id`, `name`, `group`, `path`, `sha256`, `size_bytes`).
- `GET /protocol/schema?schema_id=<id>`
  - Returns full JSON schema by id.

Registry lifecycle:
- startup generates schemas via Codex into a temporary directory, loads them into memory, then removes temporary files,
- temporary generated schema files are deleted after loading,
- requests are served from memory only.

## Long-Running Tasks (Async Jobs)

To avoid HTTP/proxy timeouts for long Codex operations, use async job endpoints:

1. `POST /codex/jobs` with body:
   - `{ "payload": { ...CodexRequest..., "diff_mode": "live|final_only|off" } }`
2. Receive `job_id` and immediate status (`queued` or `running`).
3. Poll `GET /codex/jobs/{job_id}` every `poll_after_seconds` (default 15s).
   - Prefer `retry_after_seconds` from response when present.
   - Endpoint always returns `200` for existing jobs and includes retry hints.
   - If polled too early, gateway can hold the request (long-poll) and respond later to reduce request spam.
   - Long-poll is event-driven only for significant updates (completed assistant message, turn completion, failures).
   - High-frequency WS noise (deltas/started/token-usage updates) does not wake polling early.
4. For diff orchestration:
   - If `diff_mode=live`: call `GET /codex/jobs/{job_id}/diff/live?since_version=<n>` when `diff_live_version` increases.
   - If `diff_mode=final_only`: wait for `diff_final_available=true`, then call `GET /codex/jobs/{job_id}/diff/final`.
   - If `diff_mode=off`: skip diff endpoints unless diagnostics are needed.
5. When status is `completed`, call `GET /codex/jobs/{job_id}/result`.
6. If status is `waiting_approval`, ask user and submit decision:
   - `POST /codex/jobs/{job_id}/approval`
   - body:
     - `request_id`
     - `decision`: `approve_once | approve_all_similar | deny | guidance`
     - `guidance_text` (required for `guidance`)

Notes:
- `GET /codex/jobs/{job_id}/result` returns:
  - `200` for completed jobs,
  - `409` if the job is not finished yet,
  - `502` if the job failed.
- Polling endpoints are read-only and should not require repeated user confirmations in GPT flow after job creation.
- `GET /codex/jobs/{job_id}` returns:
  - `200` with status and `retry_after_seconds` / `next_poll_after_at` hints.
  - Includes diff signals:
    - `diff_live_available`
    - `diff_live_version`
    - `diff_final_available`
    - `diff_hint`
    - `diagnostic_diff_available`
  - Includes approval signals:
    - `approval_required`
    - `approval_request`
    - `approval_policy_count`
- `GET /codex/jobs/{job_id}/diff/live`:
  - Returns only incremental diff updates after `since_version`.
- `GET /codex/jobs/{job_id}/diff/final`:
  - Returns final diff (`409` until ready).
  - On failed jobs may return diagnostic diff when available.
- `GET /codex/jobs/{job_id}/debug-trace`:
  - Returns compact WS-event trace (method + key-shape hints) to diagnose why diff extraction did or did not trigger.

### Sync endpoint fallback behavior

`POST /codex` remains available for compatibility.

For `backend=app_server_ws`, gateway now:
- validates `cwd` early; if the directory does not exist, returns `400` immediately,
- starts internal async job,
- waits up to `GATEWAY_CODEX_SYNC_MAX_WAIT_SECONDS`,
- if finished quickly: returns normal final Codex result,
- otherwise returns:
  - `status: "in_progress"`
  - `job_id`
  - current `job` status including `last_update_text` when available.
