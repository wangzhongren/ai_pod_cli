"""Multi-artifact Interface delivery generation and validation."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from ai_pod_cli.client import call_llm
from ai_pod_cli.config import append_deps_to_requirements
from ai_pod_cli.interface import verify_adapter_candidate
from ai_pod_cli.pod.routes import load_routes_map
from ai_pod_cli.pod.state import normalize_interface_plan, set_stage_status
from ai_pod_cli.security import validate_code
from ai_pod_cli.validation import (
    repair_feedback, request_repair, validate_entry_contract, validate_entry_imports,
    validate_interface_adapter_contract, validate_interface_adapter_imports,
)


def _artifact_path(raw_path: str, interface_name: str) -> Path:
    """Return one safe path rooted in interfaces/<interface-id>."""
    path = Path(str(raw_path))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Interface artifact path is unsafe: {raw_path}")
    expected_root = Path("interfaces") / interface_name
    try:
        path.relative_to(expected_root)
    except ValueError as error:
        raise ValueError(
            f"Interface artifact must be inside {expected_root.as_posix()}/: {raw_path}"
        ) from error
    return path


def _runtime_artifact(interface: dict) -> str:
    return next(
        (
            str(item.get("path")) for item in interface.get("artifacts", [])
            if isinstance(item, dict) and item.get("role") == "runtime"
        ),
        "",
    )


def _artifact_prompt(
    desc: str, interface: dict, artifact: dict, routes_map: dict[str, str],
) -> tuple[str, str]:
    path = str(artifact["path"])
    role = str(artifact.get("role", "resource"))
    artifact_format = str(artifact.get("format", "text"))
    runtime_path = _runtime_artifact(interface)
    verification = [
        item for item in interface.get("verify", [])
        if isinstance(item, dict) and path in (item.get("command") or [])
    ]
    delivery_context = {
        key: interface.get(key)
        for key in (
            "name", "kind", "platform", "instruction", "lifecycle",
            "permissions", "support", "adapter",
        )
    }
    system_prompt = f"""
    You generate exactly one artifact of an AIPod Interface delivery unit.
    Never generate another artifact and never return Markdown fences.

    Interface manifest:
    {json.dumps(delivery_context, ensure_ascii=False, indent=2)}

    Current artifact:
    - path: {path}
    - role: {role}
    - format: {artifact_format}
    - instruction: {artifact.get('instruction', '')}
    - verification checks using this artifact:
      {json.dumps(verification, ensure_ascii=False)}

    Frozen routes:
    {json.dumps(routes_map, ensure_ascii=False, indent=2)}

    Rules:
    - Return strict JSON: {{"path":"{path}","content":"complete text","extra_deps":[]}}
    - PyPI distribution name is AIPodCli; Python import name is ai_pod_cli.
    - Never import AIPodCli, the project name, the Pod name, modules, or pipelines.
    - Never import build_container, load_beans, PipelineRunner, or project Services from
      the ai_pod_cli root package. Their canonical modules are ai_pod_cli.container,
      ai_pod_cli.config, and ai_pod_cli.runner. Interface code never imports Services.
    - Legacy role=runtime Python gets PipelineRunner only through
      build_container(load_beans()).get(PipelineRunner). New role=adapter code uses only
      InterfaceContext and never accesses PipelineRunner directly.
    - An artifact with role=adapter_entry must define the manifest's adapter.class_name,
      inherit ai_pod_cli.interface.InterfaceAdapter, implement start(context, payload)
      and required_routes(), and call only context.run_route(...). It must not import
      AIPod container/runtime internals or project components.
    - role=adapter_module files contain one focused transport/UI/queue concern and use
      relative imports within the Interface bundle. They share the same import boundary.
    - Optional queue, UI, desktop, and web dependencies must be imported lazily inside
      start(); required_routes() and smoke() must not connect to external systems or open UI.
    - Runtime code must implement every declared runtime verification mode, including
      --smoke when present, without starting a long-running server or touching user data.
    - Installer and platform artifacts must call the runtime artifact at {runtime_path};
      they must not duplicate business logic.
    - A Python installer must prefer the active VIRTUAL_ENV interpreter, then a
      project-local virtual environment, then command -v python3. Never prefer or embed
      /usr/bin/python3. It must verify `import ai_pod_cli` before installing.
    - A system launcher must establish the packaged project root before starting Python
      so load_beans(), routes.toml, modules, and pipelines resolve outside the terminal's
      current working directory.
    - Do not claim native integration is installed when support.manual_steps remains.
    """
    return system_prompt, f"Application requirement:\n{desc}\n\nGenerate only {path}."


def _validate_artifact(
    interface: dict, artifact: dict, content: str, extra_deps: list[str],
    route_names: list[str],
) -> list[str]:
    path = Path(str(artifact["path"]))
    role = str(artifact.get("role", "resource"))
    artifact_format = str(artifact.get("format", "text")).lower()
    violations: list[str] = []
    if not content.strip():
        return [f"Artifact {path.as_posix()} is empty"]

    if artifact_format == "python" or path.suffix == ".py":
        if role in {"adapter", "adapter_entry"}:
            class_name = str(interface.get("adapter", {}).get("class_name", "GeneratedInterfaceAdapter"))
            violations.extend(validate_code(content, allow_file_io=True))
            violations.extend(validate_interface_adapter_contract(content, class_name))
        elif role == "adapter_module":
            violations.extend(validate_code(content, allow_file_io=True))
            violations.extend(validate_interface_adapter_imports(content))
        elif role == "runtime":
            violations.extend(validate_entry_contract(content, route_names))
        else:
            violations.extend(validate_code(content, allow_file_io=True))
        violations.extend(validate_entry_imports(content, extra_deps))
        required_smoke = any(
            isinstance(check, dict)
            and bool(check.get("required", True))
            and path.as_posix() in (check.get("command") or [])
            and "--smoke" in (check.get("command") or [])
            for check in interface.get("verify", [])
        )
        if required_smoke and "--smoke" not in content:
            violations.append(f"Runtime artifact {path.as_posix()} must implement --smoke")
    elif artifact_format == "plist" or path.suffix == ".plist":
        try:
            plistlib.loads(content.encode("utf-8"))
        except Exception as error:
            violations.append(f"Invalid plist {path.as_posix()}: {error}")
    elif artifact_format in {"shell", "sh"} or path.suffix == ".sh":
        completed = subprocess.run(
            ["sh", "-n"], input=content, capture_output=True, text=True,
        )
        if completed.returncode:
            violations.append(
                f"Invalid shell artifact {path.as_posix()}: {completed.stderr.strip()}"
            )
        if role == "installer":
            system_python = content.find("/usr/bin/python3")
            virtual_env = content.find("VIRTUAL_ENV")
            if system_python >= 0 and (virtual_env < 0 or system_python < virtual_env):
                violations.append(
                    "Installer must prefer the active VIRTUAL_ENV or project-local "
                    "environment before any system Python"
                )
            if "PYTHON_BIN" in content and "import ai_pod_cli" not in content:
                violations.append(
                    "Installer must preflight the selected interpreter with "
                    "`import ai_pod_cli` before modifying the installation"
                )
            if "main.py" in content and "cd " not in content and "AIPOD_PROJECT_ROOT" not in content:
                violations.append(
                    "Installed launcher must establish the AIPod project root before "
                    "running main.py"
                )
    return list(dict.fromkeys(violations))


def _generate_artifact(
    desc: str, interface: dict, artifact: dict, routes_map: dict[str, str],
    progress_callback=None, auto_repair: bool = False,
) -> tuple[str, list[str]] | None:
    path = str(artifact["path"])
    system_prompt, user_prompt = _artifact_prompt(desc, interface, artifact, routes_map)
    feedback = ""
    for attempt in range(1, 4):
        try:
            result = call_llm(
                system_prompt, user_prompt + feedback,
                json_mode=True, temperature=0.1, max_tokens=16384,
                progress_callback=progress_callback,
                progress_label=f"Generating Interface artifact: {path}",
            )
        except Exception as error:
            if attempt < 3:
                feedback = repair_feedback([f"Artifact model call failed: {error}"])
                continue
            print(f"   ❌ Artifact generation failed: {path}: {error}")
            return None
        returned_path = str(result.get("path", ""))
        content = str(result.get("content", result.get("code", "")))
        extra_deps = [str(item) for item in result.get("extra_deps", [])]
        violations = []
        if returned_path != path:
            violations.append(f"Artifact path must remain exactly {path}")
        violations.extend(_validate_artifact(
            interface, artifact, content, extra_deps, list(routes_map),
        ))
        if not violations:
            return content, extra_deps
        if not request_repair(
            violations, attempt, 3, interactive=not auto_repair,
            auto_repair=auto_repair,
        ):
            return None
        feedback = repair_feedback(violations)
    return None


def generate_interface_delivery(
    desc: str, interface: dict, progress_callback=None,
    auto_repair: bool = False, replace_existing: bool = False,
) -> tuple[list[str], list[str]] | None:
    """Generate and atomically install one complete Interface delivery unit."""
    holder = {"interfaces": [dict(interface)]}
    normalize_interface_plan(holder)
    interface = holder["interfaces"][0]
    name = str(interface["name"])
    artifacts = interface.get("artifacts", [])
    if not artifacts:
        print(f"   ❌ Interface {name} has no artifacts")
        return None
    try:
        paths = [_artifact_path(str(item.get("path", "")), name) for item in artifacts]
    except ValueError as error:
        print(f"   ❌ {error}")
        return None

    bundle = Path("interfaces") / name
    generated: dict[Path, str] = {}
    all_deps: list[str] = []
    routes_map = load_routes_map()
    for artifact, path in zip(artifacts, paths):
        existing = Path(path)
        if existing.is_file() and not replace_existing:
            content = existing.read_text(encoding="utf-8")
            violations = _validate_artifact(
                interface, artifact, content, [], list(routes_map),
            )
            if violations:
                print(f"   ❌ Existing artifact is invalid: {path.as_posix()}")
                for violation in violations:
                    print(f"      - {violation}")
                return None
            generated[path] = content
            continue
        result = _generate_artifact(
            desc, interface, artifact, routes_map,
            progress_callback=progress_callback, auto_repair=auto_repair,
        )
        if result is None:
            return None
        content, deps = result
        generated[path] = content
        all_deps.extend(item for item in deps if item not in all_deps)

    adapter_spec = interface.get("adapter")
    if isinstance(adapter_spec, dict) and (adapter_spec.get("entry_path") or adapter_spec.get("path")):
        adapter_path = Path(str(adapter_spec.get("entry_path") or adapter_spec.get("path")))
        adapter_sources = {
            path.as_posix(): generated[path]
            for artifact, path in zip(artifacts, paths)
            if (
                str(artifact.get("role", "")) == "adapter"
                or str(artifact.get("role", "")).startswith("adapter_")
            ) and path in generated
        }
        if adapter_path.as_posix() not in adapter_sources:
            print(f"   ❌ Adapter entry artifact was not generated: {adapter_path.as_posix()}")
            return None
        adapter_violations = verify_adapter_candidate(
            Path.cwd(), interface, adapter_sources, timeout=30,
        )
        if adapter_violations:
            print(f"   ❌ Adapter isolated verification failed: {name}")
            for violation in adapter_violations:
                print(f"      - {violation}")
            return None

    bundle.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".aipod_interface_{name}_", dir=bundle.parent,
    ) as tmp:
        staged_bundle = Path(tmp) / name
        if bundle.is_dir() and not replace_existing:
            shutil.copytree(bundle, staged_bundle)
        else:
            staged_bundle.mkdir(parents=True)
        for path, content in generated.items():
            relative = path.relative_to(bundle)
            target = staged_bundle / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        (staged_bundle / "interface.json").write_text(
            json.dumps(interface, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        backup = None
        if bundle.exists():
            backup = Path(tmp) / f"{name}.previous"
            os.replace(bundle, backup)
        try:
            os.replace(staged_bundle, bundle)
        except Exception:
            if backup is not None and backup.exists() and not bundle.exists():
                os.replace(backup, bundle)
            raise

    print(f"   ✅ [Interface delivery] {name}: {len(paths)} artifacts")
    return [path.as_posix() for path in paths], all_deps


def generate_pod_entry(
    desc: str, generated: list[str], pipe_names: list[str], progress_callback=None,
    auto_repair: bool = False, interface: dict | None = None,
    replace_existing: bool = False,
) -> tuple[str, list[str]] | None:
    """Backward-compatible wrapper returning the runtime artifact path."""
    del generated, pipe_names
    interface = interface or {}
    result = generate_interface_delivery(
        desc, interface, progress_callback, auto_repair, replace_existing,
    )
    if result is None:
        return None
    paths, deps = result
    runtime = _runtime_artifact(interface)
    return runtime or paths[0], deps


def generate_interfaces(
    *, desc: str, interfaces: list[dict], reused: list[str], generated: list[str],
    args, decision_state: dict, stage: int, progress_callback=None,
    replace_existing: bool = False,
) -> tuple[list[str], list[str]]:
    """Generate complete Interface delivery units after routes are frozen."""
    all_artifacts: list[str] = []
    available_components = list(dict.fromkeys(reused + generated))
    print(f"\n🚀 [生成 Interface deliveries] {len(interfaces)} 个")
    for index, interface in enumerate(interfaces, 1):
        print(
            f"   [{index}/{len(interfaces)}] {interface.get('name', '')}"
            f" ({interface.get('kind', '')})"
        )
        result = generate_interface_delivery(
            desc, interface, progress_callback=progress_callback,
            auto_repair=bool(getattr(args, "auto_repair", False) or args.yes),
            replace_existing=replace_existing,
        )
        if result is None:
            set_stage_status(decision_state, stage, "in_progress")
            print("   ⛔ Interface delivery 生成失败，前四阶段保持冻结。")
            raise SystemExit(1)
        paths, deps = result
        all_artifacts.extend(paths)
        if deps:
            append_deps_to_requirements(deps)
            print(f"   📦 额外依赖: {', '.join(deps)}")
    return all_artifacts, available_components
