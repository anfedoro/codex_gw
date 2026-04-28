# Codex Gateway

This repository contains a FastAPI gateway for Codex automation.

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

## GPT Action Assets

- `gpt_action_schema.template.json`: OpenAPI schema template for Custom GPT Actions.
- `gpt_system_instruction.template.txt`: system instruction template for the Custom GPT.
