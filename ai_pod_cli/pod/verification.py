"""Explicit Interface verification and evidence-bounded repair."""

import hashlib
import sys
from pathlib import Path

from ai_pod_cli.client import call_llm
from ai_pod_cli.config import load_beans
from ai_pod_cli.pod.build import _load_routes_map
from ai_pod_cli.pod.state import (
    load_decision_plan as _load_decision_plan,
    save_decision_plan as _save_decision_plan,
)
from ai_pod_cli.repair import apply_file_patches, file_patch_prompt
from ai_pod_cli.validation import (
    validate_component_contract, validate_entry_contract, validate_entry_imports,
    validate_pipeline_contract,
)

def _application_verification_specs(state: dict) -> list[dict]:
    """Read deterministic proof commands declared by frozen Interfaces."""
    verification = state["agent"]["verification"]
    existing = verification.get("command")

    interface_stage = state.get("stages", {}).get("interfaces", {})
    plan = interface_stage.get("plan") or {}
    interfaces = plan.get("interfaces", []) if isinstance(plan, dict) else []
    specs = []
    for interface in interfaces:
        if not isinstance(interface, dict):
            continue
        checks = interface.get("verify")
        if isinstance(checks, dict):
            checks = [checks]
        if not isinstance(checks, list):
            continue
        for index, verify in enumerate(checks):
            if not isinstance(verify, dict):
                continue
            command = verify.get("command")
            if not isinstance(command, list) or not command:
                continue
            resolved = [str(item) for item in command]
            if resolved[0] in {"python", "python3"}:
                resolved[0] = sys.executable
            specs.append({
                "interface": str(interface.get("name", "interface")),
                "name": str(verify.get("name", f"check_{index + 1}")),
                "kind": str(verify.get("kind", "runtime")),
                "required": bool(verify.get("required", True)),
                "command": resolved,
                "timeout": max(1, int(verify.get("timeout", 30))),
            })
    if not specs and isinstance(existing, list) and existing:
        specs.append({
            "name": "application",
            "interface": "application", "kind": "runtime", "required": True,
            "command": [str(item) for item in existing],
            "timeout": max(1, int(verification.get("timeout", 30))),
        })
    return specs


def _project_verification_fingerprint() -> str:
    """Hash behavior-relevant project files so stale passes are never reused."""
    paths = [
        path for path in (
            Path("beans_config.json"), Path("routes.toml"), Path("config.toml"),
        )
        if path.is_file()
    ]
    paths.extend(sorted(Path.cwd().glob("*.py")))
    for directory in (Path("modules"), Path("pipelines"), Path("interfaces"), Path("tests")):
        if directory.is_dir():
            paths.extend(sorted(path for path in directory.rglob("*") if path.is_file()))
    digest = hashlib.sha256()
    for path in sorted(set(paths), key=lambda item: item.as_posix()):
        digest.update(path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _verify_application(desc: str, state: dict, timeout: int | None = None) -> dict:
    """Execute every frozen Interface's explicitly declared proof command."""
    from ai_pod_cli.commands.verify import verify_project

    specs = _application_verification_specs(state)
    interface_checks = []
    required_failed = False
    suggested_files: list[str] = []
    result = verify_project([], timeout=1)
    if not specs:
        result["status"] = "failed"
        result["checks"]["execution"] = {
            "status": "failed", "exit_code": None, "command": [],
            "stdout": "", "stderr": "No Interface verification command was declared.",
            "locations": [],
        }
        result["repair"]["required"] = True
    else:
        for spec in specs:
            current = verify_project(
                spec["command"],
                timeout=max(1, int(timeout)) if timeout is not None else spec["timeout"],
            )
            interface_checks.append({
                "interface": spec["interface"], "name": spec["name"],
                "kind": spec["kind"], "required": spec["required"],
                "command": spec["command"],
                "timeout": spec["timeout"], "status": current["status"],
                "execution": current["checks"]["execution"],
            })
            result = current
            if current["status"] != "passed" and spec["required"]:
                required_failed = True
                suggested_files.extend(current.get("repair", {}).get("suggested_files", []))
        structure_failed = any(
            item.get("status") == "failed" and item.get("required")
            for item in interface_checks
            if item.get("kind") == "structure"
        )
        result["status"] = "failed" if required_failed or structure_failed else "passed"
        result["repair"]["required"] = result["status"] == "failed"
        result["repair"]["suggested_files"] = list(dict.fromkeys(suggested_files))
        result["checks"]["interfaces"] = interface_checks
    latest = _load_decision_plan(desc)
    verification = latest["agent"]["verification"]
    verification["attempts"] = int(verification.get("attempts", 0)) + 1
    verification["command"] = specs[0]["command"] if specs else []
    verification["commands"] = [spec["command"] for spec in specs]
    verification["timeout"] = specs[0]["timeout"] if specs else 0
    verification["status"] = "passed" if result["status"] == "passed" else "failed"
    verification["last_result"] = result
    verification["fingerprint"] = _project_verification_fingerprint()
    _save_decision_plan(latest)
    return result


def _validate_repaired_artifact(relative_path: str, code: str) -> list[str]:
    """Run the existing deterministic validator appropriate for one repaired file."""
    normalized = Path(relative_path).as_posix()
    if normalized.startswith("pipelines/"):
        return validate_pipeline_contract(code)

    beans = load_beans().get("beans", [])
    for bean in beans:
        class_path = str(bean.get("class_path", ""))
        if not class_path or "." not in class_path:
            continue
        module_name, class_name = class_path.rsplit(".", 1)
        component_path = Path(*module_name.split(".")).with_suffix(".py").as_posix()
        if component_path == normalized:
            return validate_component_contract(
                code,
                class_name,
                str(bean.get("category", "service")),
                bean.get("inputs"),
                bean.get("outputs"),
                bean.get("methods"),
            )
    requirements = []
    requirements_path = Path("requirements.txt")
    if requirements_path.is_file():
        requirements = [
            line.strip() for line in requirements_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    return [
        *validate_entry_contract(code, list(_load_routes_map())),
        *validate_entry_imports(code, requirements),
    ]


def _repair_current_artifact(desc: str, state: dict, progress_callback=None) -> dict:
    """Patch only the deepest project file selected by the last verification traceback."""
    from ai_pod_cli.commands.verify import _bounded_output

    verification = state["agent"]["verification"]
    result = verification.get("last_result") or {}
    suggested = result.get("repair", {}).get("suggested_files", [])
    root = Path.cwd().resolve()
    candidates: list[tuple[str, Path]] = []
    for raw_path in suggested:
        candidate = (root / str(raw_path)).resolve()
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError:
            continue
        if candidate.is_file() and candidate.suffix == ".py":
            candidates.append((relative, candidate))
    if not candidates:
        raise RuntimeError("验证失败，但没有 traceback 指向可安全修复的项目 Python 文件")

    relative_path, artifact = candidates[-1]
    source = artifact.read_text(encoding="utf-8")
    checks = result.get("checks", {})
    execution = checks.get("execution") or {}
    evidence = [
        *[str(item) for item in checks.get("structure", {}).get("issues", [])],
        str(execution.get("stdout", ""))[-8000:],
        str(execution.get("stderr", ""))[-8000:],
    ]
    evidence = [item for item in evidence if item]
    response = call_llm(
        "You repair one evidence-selected Python artifact with exact minimal patches. "
        "Never return hidden reasoning or a whole-file rewrite.",
        file_patch_prompt(_bounded_output(source, 50000), evidence, relative_path),
        json_mode=True,
        temperature=0.1,
        max_tokens=4096,
        progress_callback=progress_callback,
        progress_label=f"Repairing current artifact: {relative_path}",
    )
    repaired = apply_file_patches(source, response.get("patches"))
    violations = _validate_repaired_artifact(relative_path, repaired)
    if violations:
        raise ValueError("修复补丁未通过本地预检：" + "；".join(violations))
    artifact.write_text(repaired, encoding="utf-8")

    latest = _load_decision_plan(desc)
    latest_verification = latest["agent"]["verification"]
    latest_verification["repairs"] = int(latest_verification.get("repairs", 0)) + 1
    latest_verification["status"] = "repair_applied"
    latest_verification["repaired_file"] = relative_path
    _save_decision_plan(latest)
    return {"file": relative_path, "patch_count": len(response.get("patches", []))}
