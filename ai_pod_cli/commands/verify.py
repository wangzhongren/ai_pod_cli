"""Agent-neutral project verification with structured repair evidence."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from pathlib import PureWindowsPath

from ai_pod_cli.project_model import ProjectModelError, inspect_project


VERIFY_SCHEMA_VERSION = "1.0"
_TRACEBACK_FILE = re.compile(r'File "([^"]+)", line (\d+)')


def _project_traceback_locations(output: str, root: Path) -> list[dict]:
    locations: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for raw_path, raw_line in _TRACEBACK_FILE.findall(output):
        if PureWindowsPath(raw_path).is_absolute() and not Path(raw_path).is_absolute():
            continue
        candidate = Path(raw_path)
        path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        key = (relative, int(raw_line))
        if key not in seen:
            seen.add(key)
            locations.append({"file": relative, "line": int(raw_line)})
    return locations


def _bounded_output(value: str, limit: int = 20000) -> str:
    value = value or ""
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", value)
    value = re.sub(
        r"(?i)((?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[\"'])"
        r"[^\"'\r\n]+([\"'])",
        r"\1[REDACTED]\2",
        value,
    )
    if len(value) <= limit:
        return value
    return "[output truncated]\n" + value[-limit:]


def verify_project(command: list[str], timeout: int = 120) -> dict:
    """Return structural checks plus optional real-command execution evidence."""
    root = Path.cwd().resolve()
    try:
        project = inspect_project("project", "", False)
        project_error = None
        validation = project.get("validation", {})
    except ProjectModelError as error:
        project = None
        project_error = str(error)
        validation = {"valid": False, "issues": [project_error]}

    execution = None
    if command:
        started_command = [str(item) for item in command]
        try:
            completed = subprocess.run(
                started_command,
                cwd=root,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
            )
            combined = "\n".join(
                item for item in (completed.stdout.strip(), completed.stderr.strip()) if item
            )
            execution = {
                "status": "passed" if completed.returncode == 0 else "failed",
                "exit_code": completed.returncode,
                "command": started_command,
                "stdout": _bounded_output(completed.stdout),
                "stderr": _bounded_output(completed.stderr),
                "locations": _project_traceback_locations(combined, root),
            }
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout.decode("utf-8", "replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
            stderr = error.stderr.decode("utf-8", "replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
            execution = {
                "status": "timeout", "exit_code": None, "command": started_command,
                "stdout": _bounded_output(stdout), "stderr": _bounded_output(stderr),
                "locations": _project_traceback_locations(stdout + "\n" + stderr, root),
                "timeout_seconds": timeout,
            }
        except OSError as error:
            execution = {
                "status": "failed", "exit_code": None, "command": started_command,
                "stdout": "", "stderr": str(error), "locations": [],
            }

    structural_ok = bool(validation.get("valid"))
    execution_ok = execution is not None and execution["status"] == "passed"
    if not structural_ok or (execution is not None and not execution_ok):
        status = "failed"
    elif execution is None:
        status = "unverified"
    else:
        status = "passed"
    locations = execution.get("locations", []) if execution else []
    return {
        "schema_version": VERIFY_SCHEMA_VERSION,
        "command": "verify",
        "status": status,
        "project_root": str(root),
        "checks": {
            "structure": {
                "status": "passed" if structural_ok else "failed",
                "issues": validation.get("issues", []),
                "error": project_error,
            },
            "execution": execution,
        },
        "repair": {
            "required": status == "failed",
            "suggested_files": list(dict.fromkeys(item["file"] for item in locations)),
            "rule": "Use real evidence and make the smallest repair; preserve unrelated frozen components.",
        },
        "project": {
            "summary": project.get("summary", {}) if project else {},
            "validation": validation,
        },
    }


def handle_verify(args) -> None:
    command = list(getattr(args, "check", []) or [])
    if command and command[0] == "--":
        command = command[1:]
    result = verify_project(command, max(1, int(args.timeout)))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        structure = result["checks"]["structure"]["status"]
        execution = result["checks"]["execution"]
        print(f"🩺 AIPod verify: {result['status']}")
        print(f"   Structure: {structure}")
        if execution:
            print(f"   Command: {' '.join(execution['command'])}")
            print(f"   Execution: {execution['status']}")
            if execution.get("stderr"):
                print(execution["stderr"])
        else:
            print("   Execution: skipped (pass a command after --)")
    if result["status"] == "failed":
        raise SystemExit(1)
