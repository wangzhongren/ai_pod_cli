"""Explicit Interface verification and evidence-bounded repair."""

import ast
from copy import deepcopy
import hashlib
import json
import re
import sys
from pathlib import Path

from ai_pod_cli.client import call_llm
from ai_pod_cli.config import load_beans
from ai_pod_cli.pod.build import _load_routes_map
from ai_pod_cli.pod.state import (
    APPLICATION_PROOF_VERSION,
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
    for interface_index, interface in enumerate(interfaces):
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
            replacements = {
                "{python}": sys.executable,
                "{project_root}": str(Path.cwd().resolve()),
            }
            resolved = [replacements.get(str(item), str(item)) for item in command]
            if resolved[0] in {"python", "python3"}:
                resolved[0] = sys.executable
            specs.append({
                "interface": str(interface.get("name", "interface")),
                "interface_index": interface_index,
                "name": str(verify.get("name", f"check_{index + 1}")),
                "kind": str(verify.get("kind", "runtime")),
                "required": bool(verify.get("required", True)),
                "cases": deepcopy(verify.get("cases")),
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


def _behavior_test_path(command: list[str], *, require_file: bool = True) -> Path:
    """Accept only the framework driver and a single project-local Python test file."""
    if (
        len(command) != 4
        or command[:3] != [sys.executable, "-m", "ai_pod_cli.behavior_tests"]
    ):
        raise ValueError(
            "Behavior verification must run {python} -m ai_pod_cli.behavior_tests <test_file.py>."
        )
    root = Path.cwd().resolve()
    if Path(command[3]).is_absolute():
        raise ValueError("Behavior acceptance must name a project-relative Python test file.")
    target = (root / command[3]).resolve()
    if not target.is_relative_to(root) or target.suffix != ".py":
        raise ValueError("Behavior acceptance must use a Python test file inside the project.")
    if require_file and not target.is_file():
        raise ValueError(f"Behavior acceptance test file does not exist: {command[3]}")
    return target


def _behavior_cases(cases) -> list[dict]:
    """Read frozen named requirements without inventing acceptance scenarios."""
    if not isinstance(cases, list) or not cases:
        raise ValueError("Behavior acceptance must declare nonempty cases with test and requirement.")
    normalized = []
    seen = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Each behavior case must declare test and requirement.")
        test = case.get("test")
        requirement = case.get("requirement")
        if (
            not isinstance(test, str) or not re.fullmatch(r"[A-Za-z_]\w*\.test_\w+", test)
            or not isinstance(requirement, str) or not requirement.strip()
        ):
            raise ValueError("Behavior cases require ClassName.test_method and a nonempty requirement.")
        if test in seen:
            raise ValueError(f"Duplicate behavior acceptance case: {test}")
        seen.add(test)
        normalized.append({"test": test, "requirement": requirement})
    return normalized


def _application_verification_issues(
    state: dict, specs: list[dict] | None = None, *, require_files: bool = True,
) -> list[dict]:
    """Require explicit behavior acceptance for every Interface, including old plans."""
    specs = _application_verification_specs(state) if specs is None else specs
    plan = state.get("stages", {}).get("interfaces", {}).get("plan") or {}
    interfaces = plan.get("interfaces", []) if isinstance(plan, dict) else []
    issues = []
    if not any(isinstance(item, dict) for item in interfaces):
        return [{
            "code": "missing_behavior_verification", "interface": "application",
            "message": "No Interface declares required behavior acceptance; replan Interface verification.",
        }]
    for index, interface in enumerate(interfaces):
        if not isinstance(interface, dict):
            continue
        name = str(interface.get("name", "interface"))
        behavior = [
            item for item in specs
            if item.get("interface_index") == index
            and item.get("kind") == "behavior" and item.get("required")
        ]
        if not behavior:
            issues.append({
                "code": "missing_behavior_verification", "interface": name,
                "message": f"Interface '{name}' has no required behavior acceptance. Smoke/runtime checks are insufficient.",
            })
        for item in behavior:
            try:
                target = _behavior_test_path(item["command"], require_file=require_files)
                _behavior_cases(item.get("cases"))
                if not require_files and not target.is_file():
                    artifacts = interface.get("artifacts", [])
                    planned_test = isinstance(artifacts, list) and any(
                        isinstance(artifact, dict) and artifact.get("role") == "behavior_test"
                        and isinstance(artifact.get("path"), str)
                        and (Path.cwd() / artifact["path"]).resolve() == target
                        for artifact in artifacts
                    )
                    if not planned_test:
                        raise ValueError(
                            f"Behavior target '{item['command'][3]}' does not exist and must be declared "
                            "as a behavior_test artifact in this Interface plan."
                        )
            except ValueError as error:
                issues.append({
                    "code": "invalid_behavior_verification", "interface": name,
                    "name": item["name"], "message": str(error),
                })
    return issues


def _behavior_execution_proof(execution: dict | None, cases) -> dict:
    """Validate the driver's protocol and positive execution evidence, not just exit zero."""
    try:
        payload = json.loads((execution or {}).get("stdout", ""))
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Behavior driver did not return a valid JSON proof.") from error
    if not isinstance(payload, dict) or payload.get("schema") != APPLICATION_PROOF_VERSION:
        raise ValueError("Behavior driver proof has an unsupported schema version.")
    proof = payload.get("aipod_behavior_proof")
    if (
        not isinstance(proof, dict) or type(proof.get("version")) is not int
        or proof["version"] != 1 or proof.get("success") is not True
        or payload.get("status") != "passed"
    ):
        raise ValueError("Behavior driver did not provide a successful versioned proof.")
    for key in ("tests_run", "assertions", "route_calls"):
        if type(proof.get(key)) is not int or proof[key] < 1:
            raise ValueError(f"Behavior driver proof requires positive {key}.")
        if payload.get(key) != proof[key] or type(payload.get(key)) is not int:
            raise ValueError(f"Behavior driver proof has inconsistent {key}.")
    tests = payload.get("tests")
    if not isinstance(tests, list):
        raise ValueError("Behavior proof must include individual executed test records.")
    for case in _behavior_cases(cases):
        matching = [
            item for item in tests if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and item["id"].endswith("." + case["test"])
        ]
        if len(matching) != 1 or matching[0].get("outcome") != "passed":
            raise ValueError(f"Declared behavior case did not pass exactly once: {case['test']}")
        for key in ("assertions", "route_calls"):
            if type(matching[0].get(key)) is not int or matching[0][key] < 1:
                raise ValueError(f"Declared behavior case lacks {key}: {case['test']}")
    return payload


def _project_verification_fingerprint() -> str:
    """Hash behavior-relevant project files so stale passes are never reused."""
    paths = [
        path for path in (
            Path("beans_config.json"), Path("routes.toml"), Path("config.toml"), Path("utility_registry.json"),
        )
        if path.is_file()
    ]
    paths.extend(sorted(Path.cwd().glob("*.py")))
    for directory in (Path("modules"), Path("pipelines"), Path("interfaces"), Path("tests")):
        if directory.is_dir():
            paths.extend(sorted(path for path in directory.rglob("*") if path.is_file()))
    digest = hashlib.sha256()
    digest.update(APPLICATION_PROOF_VERSION.encode("utf-8") + b"\0")
    plan_path = Path("aipod_plan.json")
    if plan_path.is_file():
        try:
            state = json.loads(plan_path.read_text(encoding="utf-8"))
            interface_plan = state.get("stages", {}).get("interfaces", {}).get("plan") or {}
            digest.update(json.dumps(interface_plan, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\0")
            paths.extend(
                path for path in _acceptance_artifact_paths(state)
                if path.is_relative_to(Path.cwd().resolve()) and path.is_file()
            )
        except (OSError, ValueError, TypeError, KeyError):
            # An unreadable plan cannot share a prior proof fingerprint.
            digest.update(plan_path.read_bytes())
    for path in sorted(set(paths), key=lambda item: item.as_posix()):
        digest.update(path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _verify_application(desc: str, state: dict, timeout: int | None = None) -> dict:
    """Execute declared checks and require behavior evidence for every Interface."""
    from ai_pod_cli.commands.verify import verify_project

    specs = _application_verification_specs(state)
    acceptance_issues = _application_verification_issues(state, specs)
    interface_checks = []
    suggested_files: list[str] = []
    baseline = verify_project([], timeout=1)
    required_failures = []
    successful_required = []
    for spec in specs:
        effective_timeout = max(1, int(timeout)) if timeout is not None else spec["timeout"]
        invalid_command = None
        if spec["kind"] == "behavior":
            try:
                _behavior_test_path(spec["command"])
                _behavior_cases(spec.get("cases"))
            except ValueError as error:
                invalid_command = str(error)
        if invalid_command:
            current = deepcopy(baseline)
            current["status"] = "failed"
            current["checks"]["execution"] = {
                "status": "failed", "exit_code": None, "command": spec["command"],
                "stdout": "", "stderr": invalid_command, "locations": [],
            }
        else:
            current = verify_project(spec["command"], timeout=effective_timeout)
        proof = None
        proof_error = None
        if spec["kind"] == "behavior" and current["status"] == "passed":
            try:
                proof = _behavior_execution_proof(current["checks"].get("execution"), spec.get("cases"))
            except ValueError as error:
                proof_error = str(error)
                current["status"] = "failed"
                current["repair"]["required"] = True
        interface_checks.append({
            "interface": spec["interface"], "name": spec["name"],
            "kind": spec["kind"], "required": spec["required"],
            "cases": deepcopy(spec.get("cases")),
            "command": spec["command"], "timeout": effective_timeout,
            "status": current["status"],
            "execution": deepcopy(current["checks"].get("execution")),
            "structure": deepcopy(current["checks"].get("structure", {})),
            "repair": deepcopy(current.get("repair", {})),
            **({"proof": proof} if proof is not None else {}),
            **({"proof_error": proof_error} if proof_error else {}),
        })
        if spec["required"]:
            if current["status"] != "passed":
                required_failures.append(current)
                suggested_files.extend(current.get("repair", {}).get("suggested_files", []))
            else:
                successful_required.append(current)
    # A later successful or optional check must not erase a required failure's
    # traceback and diagnostics used by the repair step.
    result = deepcopy(next(iter(required_failures or successful_required), baseline))
    structure_failed = baseline["checks"]["structure"].get("status") == "failed"
    result["status"] = "failed" if acceptance_issues or required_failures or structure_failed else "passed"
    result["checks"]["interfaces"] = interface_checks
    result["checks"]["acceptance"] = {
        "status": "failed" if acceptance_issues or any(
            item["kind"] == "behavior" and item["required"] and item["status"] != "passed"
            for item in interface_checks
        ) else "passed",
        "proof_version": APPLICATION_PROOF_VERSION, "issues": acceptance_issues,
    }
    result["repair"]["required"] = result["status"] == "failed"
    result["repair"]["suggested_files"] = list(dict.fromkeys(suggested_files))
    if acceptance_issues:
        result["repair"]["action"] = "replan_interfaces"
    latest = _load_decision_plan(desc)
    verification = latest["agent"]["verification"]
    verification["attempts"] = int(verification.get("attempts", 0)) + 1
    verification["command"] = specs[0]["command"] if specs else []
    verification["commands"] = [spec["command"] for spec in specs]
    verification["timeout"] = specs[0]["timeout"] if specs else 0
    verification["status"] = result["status"]
    verification["last_result"] = result
    verification["fingerprint"] = _project_verification_fingerprint()
    verification["proof_version"] = APPLICATION_PROOF_VERSION
    if acceptance_issues:
        verification["required_action"] = "replan_interfaces"
    else:
        verification.pop("required_action", None)
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


def _acceptance_artifact_paths(state: dict) -> set[Path]:
    """Freeze declared proof targets and test artifacts independently of traceback order."""
    root = Path.cwd().resolve()
    protected = set()
    for spec in _application_verification_specs(state):
        if spec["kind"] == "behavior":
            protected.update(
                (root / str(item)).resolve() for item in spec["command"]
                if str(item).endswith(".py")
            )
    plan = state.get("stages", {}).get("interfaces", {}).get("plan") or {}
    for interface in plan.get("interfaces", []) if isinstance(plan, dict) else []:
        if not isinstance(interface, dict):
            continue
        for artifact in interface.get("artifacts", []):
            if isinstance(artifact, dict) and artifact.get("role") in {
                "behavior_test", "test", "acceptance", "acceptance_test", "assertion",
            } and artifact.get("path"):
                protected.add((root / str(artifact["path"])).resolve())
    return protected


def _is_acceptance_artifact(path: Path, relative: str, protected: set[Path]) -> bool:
    if path in protected or "tests" in Path(relative).parts:
        return True
    if path.name.startswith("test_") or path.name.endswith("_test.py"):
        return True
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return False
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        or isinstance(node, ast.ClassDef) and any(
            isinstance(base, ast.Name) and base.id == "TestCase"
            or isinstance(base, ast.Attribute) and base.attr == "TestCase"
            for base in node.bases
        )
        for node in ast.walk(tree)
    )


def _repair_current_artifact(desc: str, state: dict, progress_callback=None) -> dict:
    """Patch only the deepest project file selected by the last verification traceback."""
    from ai_pod_cli.commands.verify import _bounded_output

    verification = state["agent"]["verification"]
    result = verification.get("last_result") or {}
    if result.get("repair", {}).get("action") == "replan_interfaces":
        raise RuntimeError("缺少有效行为验收计划；必须重新规划 Interface 验收，不能通过源码修复伪造验收证据")
    suggested = result.get("repair", {}).get("suggested_files", [])
    root = Path.cwd().resolve()
    protected = _acceptance_artifact_paths(state)
    from ai_pod_cli.utility_imports import utility_catalog
    # Shared libraries have their own hash/caller-verified update transaction.
    # A one-artifact repair must not silently invalidate every other consumer.
    protected.update((root / item["path"]).resolve() for item in utility_catalog(root))
    candidates: list[tuple[str, Path]] = []
    for raw_path in suggested:
        candidate = (root / str(raw_path)).resolve()
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError:
            continue
        if (
            candidate.is_file() and candidate.suffix == ".py"
            and not _is_acceptance_artifact(candidate, relative, protected)
        ):
            candidates.append((relative, candidate))
    if not candidates:
        raise RuntimeError("验证失败，但没有 traceback 指向可安全修复的生产 Python 文件；验收文件不可修改，共享工具类须通过注册工具更新")

    relative_path, artifact = candidates[-1]
    source = artifact.read_text(encoding="utf-8")
    checks = result.get("checks", {})
    execution = checks.get("execution") or {}
    evidence = [
        *[str(item) for item in checks.get("structure", {}).get("issues", [])],
        str(execution.get("stdout", ""))[-8000:],
        str(execution.get("stderr", ""))[-8000:],
    ]
    for check in checks.get("interfaces", []):
        if not check.get("required") or check.get("status") == "passed":
            continue
        command_evidence = check.get("execution") or {}
        evidence.extend([
            f"Failed required check: {check.get('interface')}/{check.get('name')}",
            *[str(item) for item in check.get("structure", {}).get("issues", [])],
            str(command_evidence.get("stdout", ""))[-8000:],
            str(command_evidence.get("stderr", ""))[-8000:],
            str(check.get("proof_error", "")),
        ])
    evidence = [item for item in evidence if item]
    response = call_llm(
        "You repair one evidence-selected Python artifact with exact minimal patches. "
        "Never return hidden reasoning or a whole-file rewrite.",
        file_patch_prompt(_bounded_output(source, 50000), evidence, relative_path),
        json_mode=True,
        temperature=0.1,
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
