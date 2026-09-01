"""Pipeline execution state with deterministic branch isolation and merging."""

from __future__ import annotations

from copy import deepcopy
from typing import Callable, Iterable


class PipelineContext:
    """在执行链中流转的共享上下文。

    Attributes:
        params: 入口参数（由 AI 从用户指令中推断）。
        data:   组件间共享的数据池，每个组件可以读写。
        steps:  执行轨迹，记录每个组件的输入输出。
    """

    def __init__(
        self, params: dict | None = None, *, data: dict | None = None,
        branch_id: str | None = None,
    ):
        self.params: dict = params or {}
        self.data: dict = data or {}
        self.steps: list[dict] = []
        self.branch_id = branch_id
        self._fork_baseline = self._copy_mapping(self.data)

    @staticmethod
    def _copy_mapping(value: dict) -> dict:
        try:
            return deepcopy(value)
        except Exception:
            return dict(value)

    def set(self, key: str, value) -> None:
        """向数据池写入一个值。"""
        self.data[key] = value

    def get(self, key: str, default=None):
        """Read a value produced by the pipeline, falling back to entry parameters."""
        if key in self.data:
            return self.data[key]
        return self.params.get(key, default)

    def record_step(
        self,
        component_id: str,
        result,
        duration_ms: float | None = None,
        **metadata,
    ) -> None:
        """记录一个执行步骤。"""
        step = {
            "component": component_id,
            "result": result,
        }
        if duration_ms is not None:
            step["duration_ms"] = round(duration_ms, 3)
        step.update(metadata)
        self.steps.append(step)

    def fork(self, branch_id: str | None = None) -> "PipelineContext":
        """Create an isolated branch snapshot for concurrent execution."""
        return PipelineContext(
            params=self._copy_mapping(self.params),
            data=self._copy_mapping(self.data),
            branch_id=branch_id,
        )

    def changes(self) -> dict:
        """Return values written or changed since this context was forked."""
        return {
            key: value for key, value in self.data.items()
            if key not in self._fork_baseline or self._fork_baseline[key] != value
        }

    def merge(
        self,
        branches: Iterable["PipelineContext"],
        *,
        strategy: str | Callable[[str, list], object] = "strict",
    ) -> dict:
        """Merge branch-local changes in declaration order.

        ``strict`` rejects conflicting writes, ``overwrite`` lets the last branch
        win, and ``collect`` stores conflicting values as a list. A callable can
        implement an application-specific reducer and receives ``(key, values)``.
        """
        branch_list = list(branches)
        writes: dict[str, list] = {}
        for branch in branch_list:
            for key, value in branch.changes().items():
                writes.setdefault(key, []).append(value)

        merged: dict = {}
        for key, values in writes.items():
            if callable(strategy):
                merged[key] = strategy(key, values)
            elif strategy == "overwrite":
                merged[key] = values[-1]
            elif strategy == "collect":
                merged[key] = values[0] if len(values) == 1 else values
            elif strategy == "strict":
                first = values[0]
                if any(value != first for value in values[1:]):
                    raise ValueError(
                        f"parallel branches produced conflicting values for '{key}'; "
                        "declare merge='overwrite', merge='collect', or a reducer"
                    )
                merged[key] = first
            else:
                raise ValueError(f"unknown context merge strategy: {strategy}")

        self.data.update(merged)
        return merged

    def summary(self) -> dict:
        """返回执行摘要。"""
        return {
            "params": self.params,
            "data": self.data,
            "steps": [self._step_summary(step) for step in self.steps],
        }

    @staticmethod
    def _step_summary(step: dict) -> dict:
        summary = {
            "component": step["component"],
            "result_preview": str(step["result"])[:200],
        }
        for key in (
            "duration_ms", "status", "attempts", "fallback", "mode",
            "branch_count", "failure_policy", "merge", "stream_count",
            "failed_count", "last_sequence", "iterations", "stop_reason",
            "until_field", "trace_limit",
        ):
            if key in step:
                summary[key] = step[key]
        if "branches" in step:
            summary["branches"] = step["branches"]
        if "iteration_traces" in step:
            summary["iteration_traces"] = step["iteration_traces"]
        return summary

    def __repr__(self):
        return f"PipelineContext(params={self.params}, data_keys={list(self.data.keys())}, steps={len(self.steps)})"
