"""Disposable runtime checks for generated AIPod artifacts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ai_pod_cli.contracts import normalize_type


def sample_value(name: str, spec) -> object:
    """Return a deterministic, side-effect-free sample for a contract field."""
    field_type = normalize_type(spec)
    if field_type == "bool":
        return False
    if field_type == "int":
        return 1
    if field_type == "float":
        return 1.0
    if field_type.startswith("list"):
        return []
    if field_type in {"dict", "object"}:
        return {}
    lowered = name.lower()
    if "incident" in lowered:
        return "FIRE"
    if lowered == "action":
        return "status"
    return "test"


def _run(root: Path, payload: dict, timeout: int) -> list[str]:
    payload_path = root / ".aipod_sandbox_payload.json"
    payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    helper = r'''
import importlib
import json
from pathlib import Path
from ai_pod_cli.config import load_beans
from ai_pod_cli.container import Pod, build_container
from ai_pod_cli.context import PipelineContext

payload = json.loads(Path('.aipod_sandbox_payload.json').read_text(encoding='utf-8'))
beans = load_beans()
container = build_container(beans)
ctx = PipelineContext(payload.get('params', {}))

if payload['kind'] == 'provider':
    module_name, class_name = payload['class_path'].rsplit('.', 1)
    cls = getattr(importlib.import_module(module_name), class_name)
    container.get(cls)
else:
    S = Pod(container)
    by_id = {bean['id']: bean for bean in beans.get('beans', [])}
    for component_id in payload['service_ids']:
        bean = by_id[component_id]
        for key, value in payload.get('samples', {}).get(component_id, {}).items():
            if key not in ctx.data:
                ctx.params[key] = value
        module_name, class_name = bean['class_path'].rsplit('.', 1)
        cls = getattr(importlib.import_module(module_name), class_name)
        S(cls).execute_all(ctx)
        missing = [key for key in (bean.get('outputs') or {}) if key not in ctx.data]
        if missing:
            raise RuntimeError(
                f"{component_id} 运行后没有产生声明的 outputs: {', '.join(missing)}"
            )
print(json.dumps(ctx.summary(), ensure_ascii=False, default=str))
'''
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    try:
        completed = subprocess.run(
            [sys.executable, "-c", helper], cwd=root, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return [f"沙箱运行超过 {timeout} 秒；请让当前组件快速、确定性地完成测试"]
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        return ["沙箱实际运行失败：\n" + detail[-5000:]]
    return []


def verify_component_candidate(
    project_root: str | Path,
    bean: dict,
    code: str,
    service_ids: list[str],
    timeout: int = 20,
) -> list[str]:
    """Run only the current candidate after the already accepted service prefix."""
    project_root = Path(project_root).resolve()
    with tempfile.TemporaryDirectory(prefix="aipod_component_") as tmp:
        root = Path(tmp) / "project"
        shutil.copytree(project_root, root, ignore=shutil.ignore_patterns("__pycache__", ".aipod"))
        registry_path = root / "beans_config.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["beans"] = [item for item in registry["beans"] if item.get("id") != bean["id"]]
        registry["beans"].append(bean)
        registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
        category_dir = "services" if bean["category"] == "service" else "providers"
        target = root / "modules" / category_dir / bean["file"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")
        samples = {
            item["id"]: {
                name: sample_value(name, spec)
                for name, spec in (item.get("inputs") or {}).items()
            }
            for item in registry["beans"] if item.get("category") == "service"
        }
        payload = {
            "kind": bean["category"], "class_path": bean["class_path"],
            "service_ids": service_ids + ([bean["id"]] if bean["category"] == "service" else []),
            "samples": samples, "params": {},
        }
        return _run(root, payload, timeout)


def verify_pipeline_candidate(
    project_root: str | Path,
    code: str,
    contract_inputs: dict,
    timeout: int = 30,
) -> list[str]:
    """Execute a candidate Pipeline in a disposable copy of the project."""
    project_root = Path(project_root).resolve()
    with tempfile.TemporaryDirectory(prefix="aipod_pipeline_") as tmp:
        root = Path(tmp) / "project"
        shutil.copytree(project_root, root, ignore=shutil.ignore_patterns("__pycache__", ".aipod"))
        candidate = root / ".aipod_candidate_pipeline.py"
        candidate.write_text(code, encoding="utf-8")
        params = {name: sample_value(name, field) for name, field in contract_inputs.items()}
        helper = r'''
import importlib.util
import json
from pathlib import Path
from ai_pod_cli.context import PipelineContext

payload = json.loads(Path('.aipod_sandbox_payload.json').read_text(encoding='utf-8'))
spec = importlib.util.spec_from_file_location('aipod_candidate_pipeline', '.aipod_candidate_pipeline.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ctx = PipelineContext(payload['params'])
result = module.run(ctx)
print(json.dumps(result or ctx.summary(), ensure_ascii=False, default=str))
'''
        (root / ".aipod_sandbox_payload.json").write_text(
            json.dumps({"params": params}, ensure_ascii=False), encoding="utf-8",
        )
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        try:
            completed = subprocess.run(
                [sys.executable, "-c", helper], cwd=root, env=env,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return [f"Pipeline 沙箱运行超过 {timeout} 秒"]
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip()
            return ["Pipeline 沙箱实际运行失败：\n" + detail[-5000:]]
        return []
