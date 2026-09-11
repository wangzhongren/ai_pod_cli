"""Create components through the same file-owning Agent used by Pod."""

import os
from pathlib import Path

from ai_pod_cli.client import call_llm
from ai_pod_cli.config import init_config_if_not_exists
from ai_pod_cli.pod.coordinator import PodCoordinator
from ai_pod_cli.pod.state import load_current_plan, load_and_upgrade_plan, save_decision_plan
from ai_pod_cli.workspace import LAYERS
from ai_pod_cli.instruction_translator import DEFAULT_INSTRUCTION_MODE


def handle_create(args):
    if not os.environ.get("OPENAI_API_KEY"):
        from ai_pod_cli.commands.env import print_missing_model_config
        print_missing_model_config()
        raise SystemExit(1)
    init_config_if_not_exists()
    stage = args.category + "s"
    if stage not in LAYERS[:3]:
        raise ValueError("create category must be model, provider or service")
    objective = f"Create {args.category} {args.name}: {args.desc}"
    state = load_current_plan() or load_and_upgrade_plan(None, objective)
    state["agent"]["current_request"] = objective
    for upstream in LAYERS[:LAYERS.index(stage)]:
        state["stages"][upstream]["status"] = "complete"
    coordinator = PodCoordinator(Path.cwd(), state, call_llm, save=save_decision_plan,
                                 progress_callback=getattr(args, "progress_callback", None),
                                 instruction_mode=getattr(args, "instruction_mode", DEFAULT_INSTRUCTION_MODE))
    try:
        coordinator.run_layer(stage, objective, expected_component=args.name)
    except Exception:
        state["agent"]["status"] = "blocked"
        state["agent"]["verification"] = {"status": "failed"}
        coordinator.persist()
        raise
    for downstream in LAYERS[LAYERS.index(stage) + 1:]:
        state["stages"][downstream]["status"] = "pending"
    state["agent"]["verification"] = {"status": "pending"}
    coordinator.persist()
