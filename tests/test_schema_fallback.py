import json
from pathlib import Path

import codex_gateway
from gpt_action_schema_template_data import GPT_ACTION_SCHEMA_TEMPLATE_JSON


def test_embedded_action_schema_is_json_string() -> None:
    assert isinstance(GPT_ACTION_SCHEMA_TEMPLATE_JSON, str)
    parsed = json.loads(GPT_ACTION_SCHEMA_TEMPLATE_JSON)
    assert isinstance(parsed, dict)
    assert parsed.get("openapi") == "3.1.0"


def test_render_gpt_action_schema_uses_embedded_fallback(monkeypatch) -> None:
    original_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        if self.name == "gpt_action_schema.template.json":
            return False
        return original_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)
    rendered = codex_gateway._render_gpt_action_schema("https://example.test")

    assert rendered["openapi"] == "3.1.0"
    assert rendered["servers"][0]["url"] == "https://example.test"


def test_gpt_action_schema_operations_include_core_and_admin_flow() -> None:
    parsed = json.loads(GPT_ACTION_SCHEMA_TEMPLATE_JSON)
    paths = parsed.get("paths", {})
    operation_ids = set()
    for path_item in paths.values():
        if not isinstance(path_item, dict):
            continue
        for op in path_item.values():
            if not isinstance(op, dict):
                continue
            operation_id = op.get("operationId")
            if isinstance(operation_id, str):
                operation_ids.add(operation_id)

    expected = {
        "getGatewayHealth",
        "listProjects",
        "createProject",
        "listThreads",
        "getThread",
        "createThread",
        "switchProjectContext",
        "listAvailableModels",
        "selectModelForThread",
        "listSkills",
        "writeSkillConfig",
        "invokeSkill",
        "createCodexJob",
        "getCodexJob",
        "getCodexJobEvents",
        "postCodexJobApproval",
        "getCodexJobResult",
    }
    assert operation_ids == expected
    assert "getProtocolSchemas" not in operation_ids
    assert "getProtocolSchemaById" not in operation_ids
