"""AI-assisted impact classification for modifying an existing Pod."""

import json

from ai_pod_cli.client import call_llm
from ai_pod_cli.pod.state import STAGE_NAMES, stage_index


def select_revision_stage(
    instruction: str, state: dict, observation: dict,
    progress_callback=None,
) -> dict:
    """Select the earliest layer affected by a concrete modification request."""
    decision = call_llm(
        """
        You classify one requested change to an existing AIPod application.
        Select the earliest software layer whose frozen artifact must change.
        This is impact classification only; do not generate code and do not select tools.

        Layers:
        - models: shared data shape, persisted entity, field, or schema changes
        - providers: external infrastructure, database capability, queue, storage, or config access
        - services: business rules and transformations without an upstream schema change
        - pipelines: service ordering, routing, composition, retry, or fallback changes
        - interfaces: CLI, API, web, desktop, worker, presentation, or delivery-only changes

        Choose the earliest affected layer so downstream layers can be rebuilt safely.
        Return strict JSON only:
        {"stage":"models|providers|services|pipelines|interfaces","summary":"public reason"}
        """,
        "Existing objective:\n" + str(state.get("objective", ""))
        + "\n\nRequested modification:\n" + str(instruction)
        + "\n\nPublic project observation:\n"
        + json.dumps(observation, ensure_ascii=False),
        json_mode=True,
        temperature=0.0,
        max_tokens=512,
        progress_callback=progress_callback,
        progress_label="Classifying earliest affected Pod layer",
    )
    stage = str(decision.get("stage", "")).strip().lower()
    stage_index(stage)
    return {
        "stage": stage,
        "summary": str(decision.get("summary", ""))[:400],
    }
