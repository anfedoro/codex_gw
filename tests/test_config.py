from pathlib import Path

from cgw.config import GatewayConfig


def test_gateway_config_from_env(monkeypatch) -> None:
    monkeypatch.setenv("REPO", "/tmp/repo")
    monkeypatch.setenv("CODEX_TIMEOUT_SECONDS", "321")
    monkeypatch.setenv("GATEWAY_API_KEY_HEADER", "X-Token")
    monkeypatch.setenv("GATEWAY_JOB_POLL_AFTER_SECONDS", "7")
    monkeypatch.setenv("GATEWAY_PUBLIC_SCHEMA", "1")

    cfg = GatewayConfig.from_env()
    assert cfg.repo == Path("/tmp/repo").resolve()
    assert cfg.codex_timeout_seconds == 321
    assert cfg.gateway_api_key_header == "x-token"
    assert cfg.job_poll_after_seconds == 7
    assert cfg.public_schema_enabled is True
