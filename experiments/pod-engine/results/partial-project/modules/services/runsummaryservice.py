"""Run summary service for Breakout runs.

This service writes a machine-readable JSON summary describing a completed or
headless Breakout simulation.  It intentionally avoids depending on simulation
internals beyond the values stored on the PipelineContext.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from injector import inject

from ai_pod_cli.config_store import ConfigStore
from ai_pod_cli.context import PipelineContext


class RunSummaryService:
    """Service that persists a JSON summary of a finished Breakout run."""

    @inject
    def __init__(self, config_store: ConfigStore) -> None:
        self._config_store = config_store

    def execute(self, ctx: PipelineContext) -> PipelineContext:
        """Read the run state from *ctx* and write its JSON summary.

        Parameters are recovered from ``ctx.get``/``ctx.params``.  The summary
        file is written to the path that was explicitly supplied on the context
        or to the configured ``run.summary_path``.
        """
        summary_path = self._resolve_path(ctx, "summary_path", "run.summary_path")
        screenshot_path = self._resolve_path(
            ctx, "screenshot_path", "run.screenshot_path"
        )

        game_state = self._require_text(ctx, "game_state")
        state = game_state.strip().lower()
        if not state:
            raise ValueError("Context input 'game_state' cannot be empty.")

        success = state in {"won", "lost"}
        outcome = self._require_text(ctx, "outcome")
        final_state = self._require_text(ctx, "final_state")

        score = self._require_int(ctx, "score")
        lives = self._require_int(ctx, "lives")
        frame_count = self._require_int(ctx, "frame_count")
        total_bricks = self._require_int(ctx, "total_bricks")
        remaining_bricks = self._require_int(ctx, "remaining_bricks")

        ball_position = self._serialize_vec2(
            self._require_value(ctx, "ball_position"), "ball_position"
        )
        paddle_position = self._serialize_vec2(
            self._require_value(ctx, "paddle_position"), "paddle_position"
        )

        entities_raw = self._require_value(ctx, "entities")
        if not isinstance(entities_raw, (list, tuple)):
            raise ValueError("Context input 'entities' must be a list of game entities.")

        entities = [
            self._serialize_entity(entity, index)
            for index, entity in enumerate(entities_raw)
        ]

        payload = {
            "success": success,
            "outcome": outcome,
            "final_state": final_state,
            "score": score,
            "lives": lives,
            "frame_count": frame_count,
            "total_bricks": total_bricks,
            "remaining_bricks": remaining_bricks,
            "ball_position": ball_position,
            "paddle_position": paddle_position,
            "output_files": {
                "screenshot_path": screenshot_path,
                "summary_path": summary_path,
            },
            "entities": entities,
        }

        self._write_json(summary_path, payload)
        self._set_context_value(ctx, "summary_path", summary_path)

        return ctx

    def _resolve_path(
        self,
        ctx: PipelineContext,
        context_key: str,
        config_key: str,
    ) -> str:
        explicit = self._ctx_value(ctx, context_key)
        if explicit is not None and str(explicit).strip():
            return str(explicit)

        configured = self._config_store.get(config_key)
        if configured is not None and str(configured).strip():
            return str(configured)

        raise ValueError(
            f"No path for {context_key!r}: it is absent from the context and "
            f"config key {config_key!r} is not set."
        )

    def _write_json(self, path: str, payload: dict[str, Any]) -> None:
        destination = Path(path)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("w", encoding="utf-8") as summary_file:
                json.dump(payload, summary_file, indent=2, ensure_ascii=False)
                summary_file.write("\n")
        except OSError as exc:
            raise OSError(
                f"Unable to write run summary to {path!r}: {exc}"
            ) from exc

    def _require_value(self, ctx: PipelineContext, key: str) -> Any:
        value = self._ctx_value(ctx, key)
        if value is None:
            raise ValueError(f"Missing required context input: {key!r}")
        return value

    def _require_text(self, ctx: PipelineContext, key: str) -> str:
        value = self._require_value(ctx, key)
        if not isinstance(value, str):
            raise ValueError(
                f"Context input {key!r} must be a string; "
                f"got {type(value).__name__}."
            )
        return value

    def _require_int(self, ctx: PipelineContext, key: str) -> int:
        value = self._require_value(ctx, key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"Context input {key!r} must be an int; "
                f"got {type(value).__name__}."
            )
        return value

    def _serialize_vec2(self, value: Any, field_name: str) -> dict[str, Any]:
        if isinstance(value, dict):
            x = value.get("x")
            y = value.get("y")
        else:
            x = getattr(value, "x", None)
            y = getattr(value, "y", None)

        if x is None or y is None:
            raise ValueError(
                f"{field_name} must expose numeric x and y coordinates."
            )
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            raise ValueError(f"{field_name}.x must be numeric.")
        if isinstance(y, bool) or not isinstance(y, (int, float)):
            raise ValueError(f"{field_name}.y must be numeric.")

        return {"x": x, "y": y}

    def _serialize_entity(self, entity: Any, index: int) -> dict[str, Any]:
        if isinstance(entity, dict):
            entity_id = entity.get("id")
            kind = entity.get("kind")
            enabled = entity.get("enabled", True)
            position = entity.get("position")
            size = entity.get("size")
        else:
            entity_id = getattr(entity, "id", None)
            kind = getattr(entity, "kind", None)
            enabled = getattr(entity, "enabled", True)
            position = getattr(entity, "position", None)
            size = getattr(entity, "size", None)

        prefix = f"entities[{index}]"

        if not isinstance(entity_id, str):
            raise ValueError(f"{prefix}.id must be a string.")
        if not isinstance(kind, str):
            raise ValueError(f"{prefix}.kind must be a string.")
        if not isinstance(enabled, bool):
            raise ValueError(f"{prefix}.enabled must be a bool.")

        return {
            "id": entity_id,
            "kind": kind,
            "enabled": enabled,
            "position": self._serialize_vec2(position, f"{prefix}.position"),
            "size": self._serialize_vec2(size, f"{prefix}.size"),
        }

    def _ctx_value(
        self,
        ctx: PipelineContext,
        key: str,
        default: Any = None,
    ) -> Any:
        getter = getattr(ctx, "get", None)
        if callable(getter):
            try:
                value = getter(key)
            except Exception:
                value = None
            if value is not None:
                return value

        params = getattr(ctx, "params", None)
        if isinstance(params, dict) and key in params:
            return params[key]

        try:
            return getattr(ctx, key)
        except AttributeError:
            return default

    def _set_context_value(
        self,
        ctx: PipelineContext,
        key: str,
        value: Any,
    ) -> None:
        setter = getattr(ctx, "set", None)
        if callable(setter):
            setter(key, value)
            return

        params = getattr(ctx, "params", None)
        if isinstance(params, dict):
            params[key] = value
            return

        setattr(ctx, key, value)
