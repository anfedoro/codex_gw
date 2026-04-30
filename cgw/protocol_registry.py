from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


class ProtocolRegistryError(RuntimeError):
    pass


def _schema_id_from_relpath(rel_path: Path) -> str:
    return str(rel_path.with_suffix("")).replace("\\", "/")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _collect_json_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.json") if p.is_file())


def build_registry_from_dir(root: Path) -> dict[str, Any]:
    if not root.exists() or not root.is_dir():
        raise ProtocolRegistryError(f"protocol root does not exist: {root}")
    schemas_by_id: dict[str, dict] = {}
    index: list[dict] = []
    files = _collect_json_files(root)
    for p in files:
        rel = p.relative_to(root)
        schema_id = _schema_id_from_relpath(rel)
        raw = p.read_text(encoding="utf-8")
        try:
            obj = json.loads(raw)
        except Exception as exc:
            raise ProtocolRegistryError(f"invalid json schema file {p}: {exc}") from exc
        file_hash = _sha256_text(raw)
        group = rel.parts[0] if len(rel.parts) > 1 else "root"
        item = {
            "id": schema_id,
            "name": rel.name,
            "group": group,
            "path": str(rel).replace("\\", "/"),
            "sha256": file_hash,
            "size_bytes": len(raw.encode("utf-8")),
        }
        schemas_by_id[schema_id] = obj
        index.append(item)
    if not index:
        raise ProtocolRegistryError(f"no schema files found in {root}")
    registry_hash = _sha256_text(json.dumps(index, sort_keys=True, separators=(",", ":")))
    return {
        "loaded": True,
        "source": "fallback",
        "schema_count": len(index),
        "registry_hash": f"sha256:{registry_hash}",
        "index": index,
        "schemas_by_id": schemas_by_id,
        "loaded_at_epoch": int(time.time()),
        "errors": [],
    }


def build_registry_with_optional_generation(
    *,
    repo_root: Path,
    protocol_fallback_dir: Path,
    codex_bin: str = "codex",
    generate_enabled: bool = False,
) -> dict[str, Any]:
    errors: list[str] = []
    if generate_enabled:
        tmp_root = Path(os.environ.get("TMPDIR", "/tmp")).resolve()
        tmp_dir = tmp_root / f"codex-schemas-{os.getpid()}-{int(time.time())}"
        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
            cmd = [codex_bin, "app-server", "generate-json-schema", "--out", str(tmp_dir)]
            proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                errors.append(f"schema generation failed exit={proc.returncode}: {proc.stderr.strip()}")
            else:
                reg = build_registry_from_dir(tmp_dir)
                reg["source"] = "generated"
                reg["errors"] = errors
                return reg
        except Exception as exc:
            errors.append(f"schema generation exception: {exc}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    reg = build_registry_from_dir(protocol_fallback_dir)
    reg["source"] = "fallback"
    reg["errors"] = errors
    return reg

