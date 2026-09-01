"""Run and manage AI-generated Interface adapters through the stable SDK."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from ai_pod_cli.interface import (
    InterfaceError, create_context, find_project_root, load_adapter, load_manifest,
)


def _expanded_command(command: list[str], project_root) -> list[str]:
    replacements = {
        "{python}": sys.executable,
        "{project_root}": str(project_root),
    }
    return [replacements.get(str(item), str(item)) for item in command]


def _run_lifecycle(manifest: dict, name: str, project_root) -> dict:
    command = manifest.get("lifecycle", {}).get(name, [])
    if not isinstance(command, list) or not command:
        return {"status": "skipped", "lifecycle": name, "reason": "not declared"}
    expanded = _expanded_command(command, project_root)
    env = os.environ.copy()
    env["AIPOD_PROJECT_ROOT"] = str(project_root)
    env["AIPOD_PYTHON"] = sys.executable
    completed = subprocess.run(
        expanded, cwd=project_root, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", shell=False,
    )
    return {
        "status": "passed" if completed.returncode == 0 else "failed",
        "lifecycle": name, "command": expanded, "exit_code": completed.returncode,
        "stdout": completed.stdout, "stderr": completed.stderr,
    }


def handle_interface(args):
    root = find_project_root(args.project_root or ".")
    if args.action == "list":
        result = {
            "interfaces": [
                path.parent.name for path in sorted((root / "interfaces").glob("*/interface.json"))
            ]
        }
    else:
        _path, manifest = load_manifest(args.target, root)
        if args.action in {"install", "uninstall"}:
            result = _run_lifecycle(manifest, args.action, root)
        else:
            adapter = load_adapter(manifest, root)
            context = create_context(manifest, root)
            if args.action == "run":
                inputs = list(getattr(args, "inputs", []) or [])
                if inputs and inputs[0] == "--":
                    inputs = inputs[1:]
                payload = inputs if inputs else json.loads(args.payload or "{}")
                result = adapter.start(context, payload)
            elif args.action == "smoke":
                result = adapter.smoke(context)
            else:
                raise InterfaceError(f"Unsupported Interface action: {args.action}")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
