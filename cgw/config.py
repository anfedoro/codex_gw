from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class GatewayConfig:
    repo: Path
    codex_timeout_seconds: int
    app_server_url: str
    app_server_bearer_token: str | None
    app_server_timeout_seconds: int
    gateway_api_key: str
    gateway_api_key_header: str
    auth_disabled: bool
    debug_mode: bool
    log_level: str
    log_file: str
    log_requests: bool
    max_output_chars: int
    job_poll_after_seconds: int
    job_ttl_seconds: int
    job_max_items: int
    job_long_poll_enabled: bool
    job_long_poll_max_seconds: int
    codex_sync_max_wait_seconds: int
    job_debug_trace_enabled: bool
    job_debug_trace_max_items: int
    approval_poll_interval_seconds: float
    public_schema_enabled: bool
    schema_import_key: str
    schema_import_key_param: str

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        return cls(
            repo=Path(os.environ.get("REPO", os.getcwd())).resolve(),
            codex_timeout_seconds=int(os.environ.get("CODEX_TIMEOUT_SECONDS", "120")),
            app_server_url=os.environ.get("APP_SERVER_URL", "ws://127.0.0.1:4500"),
            app_server_bearer_token=os.environ.get("APP_SERVER_BEARER_TOKEN"),
            app_server_timeout_seconds=int(os.environ.get("APP_SERVER_TIMEOUT_SECONDS", "180")),
            gateway_api_key=os.environ.get("CODEX_GATEWAY_API_KEY", "").strip(),
            gateway_api_key_header=os.environ.get("GATEWAY_API_KEY_HEADER", "x-api-key").strip().lower(),
            auth_disabled=os.environ.get("GATEWAY_DISABLE_AUTH", "0") == "1",
            debug_mode=os.environ.get("GATEWAY_DEBUG", "0") == "1",
            log_level=os.environ.get("GATEWAY_LOG_LEVEL", "INFO").upper(),
            log_file=os.environ.get("GATEWAY_LOG_FILE", "").strip(),
            log_requests=os.environ.get("GATEWAY_LOG_REQUESTS", "1") == "1",
            max_output_chars=int(os.environ.get("GATEWAY_MAX_OUTPUT_CHARS", "60000")),
            job_poll_after_seconds=int(os.environ.get("GATEWAY_JOB_POLL_AFTER_SECONDS", "15")),
            job_ttl_seconds=int(os.environ.get("GATEWAY_JOB_TTL_SECONDS", "7200")),
            job_max_items=int(os.environ.get("GATEWAY_JOB_MAX_ITEMS", "500")),
            job_long_poll_enabled=os.environ.get("GATEWAY_JOB_LONG_POLL_ENABLED", "1") == "1",
            job_long_poll_max_seconds=int(os.environ.get("GATEWAY_JOB_LONG_POLL_MAX_SECONDS", "20")),
            codex_sync_max_wait_seconds=int(os.environ.get("GATEWAY_CODEX_SYNC_MAX_WAIT_SECONDS", "20")),
            job_debug_trace_enabled=os.environ.get("GATEWAY_JOB_DEBUG_TRACE_ENABLED", "1") == "1",
            job_debug_trace_max_items=int(os.environ.get("GATEWAY_JOB_DEBUG_TRACE_MAX_ITEMS", "400")),
            approval_poll_interval_seconds=float(os.environ.get("GATEWAY_APPROVAL_POLL_INTERVAL_SECONDS", "0.25")),
            public_schema_enabled=os.environ.get("GATEWAY_PUBLIC_SCHEMA", "0") == "1",
            schema_import_key=os.environ.get("GATEWAY_SCHEMA_IMPORT_KEY", "").strip(),
            schema_import_key_param=os.environ.get("GATEWAY_SCHEMA_IMPORT_KEY_PARAM", "schema_key").strip(),
        )

