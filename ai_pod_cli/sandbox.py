"""Disposable runtime checks for generated AIPod artifacts."""

from __future__ import annotations

import json
import importlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ai_pod_cli.contracts import normalize_type


def sample_value(name: str, spec) -> object:
    """Return a deterministic, side-effect-free sample for a contract field."""
    lowered = name.lower()
    if isinstance(spec, dict) and isinstance(spec.get("enum"), list) and spec["enum"]:
        return spec["enum"][0]
    if isinstance(spec, str):
        # Keep domain validation intact by using a value explicitly advertised by
        # the Contract instead of the generic string "test".  AI-generated
        # contracts commonly describe enums as 'IN' | 'OUT' | 'ADJUST'.
        quoted_choices = re.findall(r"['\"]([^'\"]+)['\"]", spec)
        if len(quoted_choices) >= 2:
            return quoted_choices[0]
        upper_choices = re.findall(r"\b[A-Z][A-Z0-9_]{1,}\b", spec)
        if len(upper_choices) >= 2:
            return upper_choices[0]
    if lowered in {"sql", "query", "statement"}:
        return "SELECT 1"
    if lowered in {"params", "parameters", "bindings", "args"}:
        return []
    field_type = normalize_type(spec)
    if "|" in field_type:
        field_type = next(
            (item for item in field_type.split("|") if item not in {"none", "null"}),
            "any",
        )
    if lowered in {
        "dt", "datetime", "timestamp", "time_value", "period_start", "period_end",
        "start_time", "end_time",
    }:
        moment = datetime(2024, 1, 1, tzinfo=timezone.utc)
        return moment if field_type in {"datetime", "datetime.datetime"} else moment.isoformat()
    if lowered.endswith(("_minutes", "_days", "_seconds", "_count", "_size", "_limit")):
        return 1.0 if field_type == "float" else 1
    if field_type in {"datetime", "datetime.datetime"}:
        return datetime(2024, 1, 1, tzinfo=timezone.utc)
    if field_type == "model" and isinstance(spec, dict):
        module_name, class_name = spec["model"].rsplit(".", 1)
        model_class = getattr(importlib.import_module(module_name), class_name)
        return model_class.sample_instance()
    if field_type == "bool":
        return False
    if field_type == "int":
        return 1
    if field_type == "float":
        return 1.0
    if field_type.startswith("list"):
        if isinstance(spec, dict) and "items" in spec:
            return [sample_value(name, spec["items"])]
        return []
    if field_type in {"dict", "object"}:
        if isinstance(spec, dict):
            if "additionalProperties" in spec:
                return {"sample": sample_value("value", spec["additionalProperties"])}
            properties = spec.get("properties", {})
            required = spec.get("required", [])
            if isinstance(properties, dict) and isinstance(required, list):
                return {
                    key: sample_value(key, properties.get(key, {}))
                    for key in required
                }
        return {}
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
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from ai_pod_cli.config import load_beans
from ai_pod_cli.container import Pod, build_container
from ai_pod_cli.context import PipelineContext
from ai_pod_cli.contracts import validate_contract_data, validate_contract_value
from ai_pod_cli.sandbox import sample_value

payload = json.loads(Path('.aipod_sandbox_payload.json').read_text(encoding='utf-8'))
beans = load_beans()
container = build_container(beans)
ctx = PipelineContext(payload.get('params', {}))

# Services commonly operate on an existing entity selected by an ``*_id``
# input.  Seed one deterministic row per frozen SQLModel so the sandbox tests
# real business execution instead of failing immediately on an empty database.
if payload['kind'] == 'service':
    from ai_pod_cli.repository import ModelRepository
    repository = container.get(ModelRepository)
    for model_bean in (item for item in beans.get('beans', []) if item.get('category') == 'model'):
        module_name, class_name = model_bean['class_path'].rsplit('.', 1)
        model_class = getattr(importlib.import_module(module_name), class_name)
        sample = model_class.sample_instance()
        object_id = getattr(sample, 'id', None)
        if object_id is None or repository.get(model_class, object_id) is None:
            repository.save(sample)

if payload['kind'] in ('provider', 'model'):
    module_name, class_name = payload['class_path'].rsplit('.', 1)
    cls = getattr(importlib.import_module(module_name), class_name)
    if payload['kind'] == 'provider':
        instance = container.get(cls)
        for method_name, method in payload.get('provider_methods', {}).items():
            method_inputs = {
                name: sample_value(name, spec)
                for name, spec in method.get('input_specs', {}).items()
            }
            result = getattr(instance, method_name)(**method_inputs)
            errors = validate_contract_value(
                result, method.get('output_spec', 'any'),
                f"{payload['class_path']}.{method_name}.return",
            )
            if errors:
                raise RuntimeError("provider method output validation failed: " + "; ".join(errors))
else:
    S = Pod(container)
    by_id = {bean['id']: bean for bean in beans.get('beans', [])}
    for component_id in payload['service_ids']:
        bean = by_id[component_id]
        for key, spec in payload.get('sample_specs', {}).get(component_id, {}).items():
            if key not in ctx.data:
                ctx.params[key] = sample_value(key, spec)
        current = {**ctx.params, **ctx.data}
        input_errors = validate_contract_data(current, bean.get('inputs') or {}, component_id)
        if input_errors:
            raise RuntimeError(
                f"{component_id} inputs schema validation failed: " + "; ".join(input_errors)
            )
        module_name, class_name = bean['class_path'].rsplit('.', 1)
        cls = getattr(importlib.import_module(module_name), class_name)
        S(cls).execute_all(ctx)
        output_errors = validate_contract_data(
            ctx.data, bean.get('outputs') or {}, component_id
        )
        if output_errors:
            raise RuntimeError(
                f"{component_id} outputs schema validation failed: " + "; ".join(output_errors)
            )
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
        modules_init = root / "modules" / "__init__.py"
        modules_init.parent.mkdir(parents=True, exist_ok=True)
        modules_init.touch(exist_ok=True)
        registry_path = root / "beans_config.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["beans"] = [item for item in registry["beans"] if item.get("id") != bean["id"]]
        registry["beans"].append(bean)
        registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
        category_dir = {
            "model": "models", "provider": "providers", "service": "services",
        }[bean["category"]]
        target = root / "modules" / category_dir / bean["file"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")
        sample_specs = {
            item["id"]: (item.get("inputs") or {})
            for item in registry["beans"] if item.get("category") == "service"
        }
        payload = {
            "kind": bean["category"], "class_path": bean["class_path"],
            "service_ids": service_ids + ([bean["id"]] if bean["category"] == "service" else []),
            "sample_specs": sample_specs, "params": {},
        }
        if bean["category"] == "provider":
            payload["provider_methods"] = {
                method_name: {
                    "input_specs": method.get("inputs") or {},
                    "output_spec": method.get("outputs", "any"),
                }
                for method_name, method in (bean.get("methods") or {}).items()
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
        modules_init = root / "modules" / "__init__.py"
        modules_init.parent.mkdir(parents=True, exist_ok=True)
        modules_init.touch(exist_ok=True)
        candidate = root / ".aipod_candidate_pipeline.py"
        candidate.write_text(code, encoding="utf-8")
        helper = r'''
import importlib.util
import json
from pathlib import Path
from ai_pod_cli.context import PipelineContext
from ai_pod_cli.sandbox import sample_value

payload = json.loads(Path('.aipod_sandbox_payload.json').read_text(encoding='utf-8'))
spec = importlib.util.spec_from_file_location('aipod_candidate_pipeline', '.aipod_candidate_pipeline.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ctx = PipelineContext({
    name: sample_value(name, spec)
    for name, spec in payload['input_specs'].items()
})
result = module.run(ctx)
print(json.dumps(result or ctx.summary(), ensure_ascii=False, default=str))
'''
        (root / ".aipod_sandbox_payload.json").write_text(
            json.dumps({"input_specs": contract_inputs}, ensure_ascii=False), encoding="utf-8",
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
