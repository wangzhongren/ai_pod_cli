"""Stable JSON envelopes for mutating commands used by AI agents."""

import io
import json
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path

import tomlkit

from ai_pod_cli.config import CONFIG_FILE, CONFIG_TOML, ROUTES_TOML


AGENT_OUTPUT_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class ProjectSnapshot:
    components: dict[str, dict]
    routes: dict[str, dict]
    config: dict


def _read_json(path: str, default: dict) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _read_toml(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return dict(tomlkit.load(f))
    except (OSError, tomlkit.exceptions.TOMLKitError):
        return {}


def project_snapshot() -> ProjectSnapshot:
    """Capture only agent-relevant project state before or after a command."""
    beans = _read_json(CONFIG_FILE, {"beans": []}).get("beans", [])
    components = {bean.get("id", ""): bean for bean in beans if bean.get("id")}
    return ProjectSnapshot(components=components, routes=_read_toml(ROUTES_TOML), config=_read_toml(CONFIG_TOML))


def snapshot_diff(before: ProjectSnapshot, after: ProjectSnapshot) -> dict:
    """Return deterministic changes without requiring an agent to parse logs."""
    def changed(before_map: dict, after_map: dict) -> dict:
        return {
            "added": sorted(set(after_map) - set(before_map)),
            "removed": sorted(set(before_map) - set(after_map)),
            "updated": sorted(key for key in set(before_map) & set(after_map) if before_map[key] != after_map[key]),
        }

    return {
        "components": changed(before.components, after.components),
        "routes": changed(before.routes, after.routes),
        "config_changed": before.config != after.config,
    }


def execute_json_command(command: str, handler, args) -> None:
    """Run a legacy command silently and emit one machine-readable result only."""
    before = project_snapshot()
    stream = io.StringIO()
    error = None
    exit_code = 0
    try:
        with redirect_stdout(stream):
            handler(args)
    except SystemExit as raised:
        exit_code = int(raised.code) if isinstance(raised.code, int) else 1
        error = {"type": "SystemExit", "message": stream.getvalue().strip() or "命令提前退出"}
    except Exception as raised:  # Preserve a stable error shape for agent recovery.
        exit_code = 1
        error = {"type": type(raised).__name__, "message": str(raised)}

    after = project_snapshot()
    changes = snapshot_diff(before, after)
    changed = changes["config_changed"] or any(changes[group][kind] for group in ("components", "routes") for kind in ("added", "removed", "updated"))
    mutating_request = not (command == "compose" and getattr(args, "list", False))
    payload = {
        "schema_version": AGENT_OUTPUT_SCHEMA_VERSION,
        "command": command,
        "status": "failed" if error else ("completed" if changed or not mutating_request else "no_change"),
        "exit_code": exit_code,
        "changes": changes,
        "project": {
            "component_count": len(after.components),
            "route_count": len(after.routes),
        },
        "error": error,
        "diagnostics": [line for line in stream.getvalue().splitlines() if line.strip()],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
