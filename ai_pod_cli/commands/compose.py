"""`compose` command — AI generates pipeline file and registers it in routes.toml."""

import json
import os
import re
from pathlib import Path

from ai_pod_cli.client import call_llm
from ai_pod_cli.config import PIPELINES_DIR


def _list_pipelines() -> list[dict]:
    """列出所有已保存的 pipeline (.py 文件)。"""
    if not os.path.exists(PIPELINES_DIR):
        return []

    pipelines = []
    for f in sorted(os.listdir(PIPELINES_DIR)):
        if f == "__init__.py":
            continue

        filepath = os.path.join(PIPELINES_DIR, f)

        if f.endswith(".py"):
            instruction = ""
            try:
                with open(filepath, "r", encoding="utf-8") as fh:
                    first_lines = "".join(fh.readline() for _ in range(3))
                match = re.search(r'Pipeline:\s*(.+)', first_lines)
                if match:
                    instruction = match.group(1).strip()
            except Exception:
                pass

            pipelines.append({
                "file": f,
                "name": f.replace(".py", ""),
                "instruction": instruction,
            })

    return pipelines


def handle_compose(args):
    """Compose through the same file-owning Pipeline Agent used by Pod."""
    if getattr(args, "list", False):
        result = _list_pipelines()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    if not os.environ.get("OPENAI_API_KEY"):
        from ai_pod_cli.commands.env import print_missing_model_config
        print_missing_model_config()
        return False
    from ai_pod_cli.config import init_config_if_not_exists
    from ai_pod_cli.pod.coordinator import PodCoordinator
    from ai_pod_cli.pod.state import load_current_plan, load_and_upgrade_plan, save_decision_plan
    from ai_pod_cli.workspace import LAYERS
    init_config_if_not_exists()
    objective = f"Compose route {getattr(args, 'name', '')}: {args.cmd}"
    state = load_current_plan() or load_and_upgrade_plan(None, objective)
    state["agent"]["current_request"] = objective
    for stage in LAYERS[:3]:
        state["stages"][stage]["status"] = "complete"
    coordinator = PodCoordinator(Path.cwd(), state, call_llm, save=save_decision_plan,
                                 progress_callback=getattr(args, "progress_callback", None))
    try:
        coordinator.run_layer("pipelines", objective)
    except Exception as error:
        state["agent"]["status"] = "blocked"
        state["agent"]["verification"] = {"status": "failed"}
        coordinator.persist()
        print(f"Pipeline Agent failed: {error}")
        return False
    state["stages"]["interfaces"]["status"] = "pending"
    state["agent"]["verification"] = {"status": "pending"}
    coordinator.persist()
    return True
