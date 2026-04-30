import json
from pathlib import Path

from cgw.protocol_registry import build_registry_from_dir


def test_build_registry_from_dir(tmp_path: Path) -> None:
    protocol_dir = tmp_path / "protocol"
    protocol_dir.mkdir()
    (protocol_dir / "A.json").write_text(json.dumps({"type": "object"}), encoding="utf-8")
    sub = protocol_dir / "v2"
    sub.mkdir()
    (sub / "B.json").write_text(json.dumps({"title": "B"}), encoding="utf-8")

    reg = build_registry_from_dir(protocol_dir)
    assert reg["loaded"] is True
    assert reg["schema_count"] == 2
    assert "A" in reg["schemas_by_id"]
    assert "v2/B" in reg["schemas_by_id"]
    ids = {x["id"] for x in reg["index"]}
    assert {"A", "v2/B"} <= ids

