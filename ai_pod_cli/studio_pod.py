"""Studio background Pod-build task service."""

import re
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from ai_pod_cli.project_model import ProjectModelError, build_project_model
from ai_pod_cli.pod.state import load_current_plan
from ai_pod_cli.studio_common import (
    PodCancelled, ProgressCapture, StudioError, redirect_current_thread_stdout,
)


_POD_PROGRESS_RANGES = {
    "impact_analysis": (2, 9),
    "models": (10, 24),
    "providers": (25, 39),
    "services": (40, 59),
    "pipelines": (60, 76),
    "interfaces": (77, 91),
    "verification": (92, 99),
}
_POD_PROGRESS_ORDER = tuple(_POD_PROGRESS_RANGES)
_POD_PROGRESS_ALIASES = {
    "planning": "impact_analysis",
    "planned": "impact_analysis",
    "components": "services",
    "entrypoint": "interfaces",
    "validation": "verification",
    "repair": "verification",
}


def _progress_phase(stage: str) -> str:
    return _POD_PROGRESS_ALIASES.get(stage, stage)


def _phase_percent(stage: str, fraction: float = 0.0) -> int:
    phase = _progress_phase(stage)
    start, end = _POD_PROGRESS_RANGES[phase]
    fraction = min(1.0, max(0.0, float(fraction)))
    return start + int((end - start) * fraction)


def _apply_phase_progress(task: dict, stage: str, percent: int) -> None:
    """Keep total progress inside the current phase's reserved interval."""
    phase = _progress_phase(stage)
    if phase not in _POD_PROGRESS_RANGES:
        return
    start, end = _POD_PROGRESS_RANGES[phase]
    bounded = min(end, max(start, int(percent)))
    current_phase = _progress_phase(str(task.get("stage", "planning")))
    if current_phase in _POD_PROGRESS_ORDER:
        current_index = _POD_PROGRESS_ORDER.index(current_phase)
        next_index = _POD_PROGRESS_ORDER.index(phase)
        if next_index < current_index:
            return
        if next_index == current_index:
            bounded = max(min(end, int(task.get("percent", start))), bounded)
    task["stage"] = stage
    task["percent"] = bounded


class StudioPodService:
    def start_pod_build(self, description: str, stage: str = "") -> dict:
        """Start one Pod build in the background and return its task id."""
        description = str(description).strip()
        if not description:
            return self._error(StudioError("请描述你想构建的程序"))
        stage = str(stage or "").strip().lower()
        if not stage:
            with self._in_project():
                current = load_current_plan()
            if current is not None and current.get("objective") != description:
                stage = "auto"
        with self._pod_task_lock:
            active = next(
                (task for task in self._pod_tasks.values() if task["status"] in {"running", "cancelling"}),
                None,
            )
            if active:
                return {"ok": False, "error": {"type": "BuildInProgress", "message": "A Pod build is already running."}, "build_id": active["build_id"]}
            build_id = f"pod_{uuid4().hex[:12]}"
            task = {
                "build_id": build_id, "status": "running", "stage": "planning",
                "percent": 2, "message": "Preparing project context…", "logs": [],
                "result": None, "error": None, "cancel_requested": False,
                "received_characters": 0,
                "requested_stage": str(stage or ""),
                "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None,
            }
            self._pod_tasks[build_id] = task
        threading.Thread(
            target=self._run_pod_task,
            args=(build_id, description, str(stage or "")),
            name=f"aipod-build-{build_id}",
            daemon=True,
        ).start()
        return {"ok": True, "build_id": build_id, "task": self._pod_task_snapshot(task)}

    def pod_build_status(self, build_id: str) -> dict:
        """Return a non-blocking snapshot for a background Pod build."""
        with self._pod_task_lock:
            task = self._pod_tasks.get(str(build_id))
            if task is None:
                return self._error(StudioError("Pod build task not found"))
            return {"ok": True, "task": self._pod_task_snapshot(task)}

    def cancel_pod_build(self, build_id: str) -> dict:
        """Request cooperative cancellation of a running Pod build."""
        with self._pod_task_lock:
            task = self._pod_tasks.get(str(build_id))
            if task is None:
                return self._error(StudioError("Pod build task not found"))
            if task["status"] == "running":
                task["cancel_requested"] = True
                task["status"] = "cancelling"
                task["message"] = "Cancellation requested; waiting for the current model call…"
            return {"ok": True, "task": self._pod_task_snapshot(task)}

    def build_pod(
        self, description: str, _progress=None, _cancelled=None, stage: str = "",
    ) -> dict:
        """Run the canonical Pod workflow from one natural-language requirement."""
        try:
            description = str(description).strip()
            if not description:
                raise StudioError("请描述你想构建的程序")
            with self._in_project():
                before = build_project_model()
                before_entries = set(self._discover_entrypoints())
                from ai_pod_cli.commands.pod import handle_pod
                output = ProgressCapture(_progress, _cancelled)
                args = SimpleNamespace(
                    desc=description, file="", yes=True, json=True,
                    auto_repair=True, stage=str(stage or ""),
                    progress_callback=getattr(_progress, "event", None),
                )
                with redirect_current_thread_stdout(output):
                    try:
                        handle_pod(args)
                    except SystemExit as error:
                        raise StudioError(output.getvalue().strip() or f"Pod 命令退出：{error.code}") from error
                after = build_project_model()
                before_components = {item["id"] for item in before["components"]}
                before_pipelines = {item["name"] for item in before["pipelines"]}
                added_components = [item["id"] for item in after["components"] if item["id"] not in before_components]
                added_pipelines = [item["name"] for item in after["pipelines"] if item["name"] not in before_pipelines]
                added_entries = [item for item in self._discover_entrypoints() if item not in before_entries]
                diagnostics = [line for line in output.getvalue().splitlines() if line.strip()]
                reused_components = []
                reused_pipelines = []
                for line in diagnostics:
                    component_match = re.search(r"♻️\s+([A-Za-z][A-Za-z0-9_]*)\s+\(reuse\)", line)
                    pipeline_match = re.search(r"\[Pipeline 复用\]\s+([A-Za-z][A-Za-z0-9_-]*)", line)
                    if component_match and component_match.group(1) not in reused_components:
                        reused_components.append(component_match.group(1))
                    if pipeline_match and pipeline_match.group(1) not in reused_pipelines:
                        reused_pipelines.append(pipeline_match.group(1))
                verification_status = (
                    after.get("pod_agent", {}).get("verification", {}).get("status")
                )
                if (
                    not added_components and not added_pipelines
                    and not reused_components and not reused_pipelines
                    and verification_status != "passed"
                ):
                    message = diagnostics[-1] if diagnostics else "Pod 没有生成项目内容，请检查模型配置"
                    raise StudioError(message)
            project = self.inspect_project()["project"]
            return {
                "ok": True,
                "project": project,
                "changes": {
                    "components": added_components, "pipelines": added_pipelines,
                    "entrypoints": added_entries, "reused_components": reused_components,
                    "reused_pipelines": reused_pipelines,
                },
                "diagnostics": diagnostics,
            }
        except (OSError, StudioError, ValueError, ProjectModelError) as error:
            return self._error(error)

    def _run_pod_task(self, build_id: str, description: str, stage: str = "") -> None:
        def cancelled():
            with self._pod_task_lock:
                return bool(self._pod_tasks[build_id]["cancel_requested"])

        def progress(line: str):
            self._record_pod_progress(build_id, line)

        def event(payload: dict):
            if cancelled():
                raise PodCancelled()
            self._record_pod_event(build_id, payload)

        progress.event = event

        try:
            result = (
                self.build_pod(description, progress, cancelled, stage)
                if stage else self.build_pod(description, progress, cancelled)
            )
            with self._pod_task_lock:
                task = self._pod_tasks[build_id]
                if task["cancel_requested"]:
                    task.update(status="cancelled", stage="cancelled", message="Pod build cancelled.")
                elif result.get("ok"):
                    task.update(status="completed", stage="completed", percent=100, message="Pod build completed.", result=result)
                else:
                    task.update(status="failed", stage="failed", message=result.get("error", {}).get("message", "Pod build failed."), error=result.get("error"))
                task["finished_at"] = datetime.now(timezone.utc).isoformat()
        except PodCancelled:
            with self._pod_task_lock:
                task = self._pod_tasks[build_id]
                task.update(status="cancelled", stage="cancelled", message="Pod build cancelled.", finished_at=datetime.now(timezone.utc).isoformat())
        except BaseException as error:
            with self._pod_task_lock:
                task = self._pod_tasks[build_id]
                task.update(
                    status="failed", stage="failed", message=str(error),
                    error={"type": type(error).__name__, "message": str(error)},
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )

    def _record_pod_progress(self, build_id: str, line: str) -> None:
        clean = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
        if not clean:
            return
        if len(clean) > 2_000:
            clean = clean[:2_000] + " … [truncated]"
        stage, percent, message = None, 0, clean
        match = re.search(
            r"\[(\d+)/(\d+)\]\s*生成\s+([^\s]+)\s+\((model|provider|service)\)",
            clean,
        )
        frozen = re.search(
            r"\[(models|providers|services|pipelines|interfaces)\s+阶段已冻结\]",
            clean,
        )
        if "verify_application" in clean:
            stage, percent, message = "verification", _phase_percent("verification", 0.55), "Running application verification."
        elif "repair_current_artifact" in clean:
            stage, percent, message = "repair", _phase_percent("verification", 0.72), "Repairing the current failing artifact."
        elif "拆解方案" in clean:
            message = "Architecture planned."
        elif frozen:
            stage = frozen.group(1)
            percent = _phase_percent(stage, 1.0)
            message = f"{stage.capitalize()} stage completed."
        elif match:
            current, total, name = int(match.group(1)), max(1, int(match.group(2))), match.group(3)
            stage = {"model": "models", "provider": "providers", "service": "services"}[match.group(4)]
            percent = _phase_percent(stage, (current - 1) / total)
            message = f"Generating component {current}/{total}: {name}"
        elif "[生成 Pipeline]" in clean or "[Pipeline 复用]" in clean:
            stage, percent = "pipelines", _phase_percent("pipelines", 0.25)
        elif "[Interface delivery]" in clean:
            stage, percent = "interfaces", _phase_percent("interfaces", 0.75)
        with self._pod_task_lock:
            task = self._pod_tasks.get(build_id)
            if task is None:
                return
            task["logs"].append(clean)
            if len(task["logs"]) > 500:
                del task["logs"][:100]
            if task["status"] == "running":
                if stage:
                    _apply_phase_progress(task, stage, percent)
                task["message"] = message[:300]

    def _record_pod_event(self, build_id: str, event: dict) -> None:
        """Record model-stream activity without retaining generated content."""
        label = str(event.get("label", "Model response"))[:180]
        characters = max(0, int(event.get("characters", 0)))
        event_type = str(event.get("type", "llm_delta"))
        with self._pod_task_lock:
            task = self._pod_tasks.get(build_id)
            if task is None or task["status"] not in {"running", "cancelling"}:
                return
            task["received_characters"] = characters
            if task["status"] == "running":
                stage = task["stage"]
                percent = task["percent"]
                component_match = re.match(r"Generating component (\d+)/(\d+):", label)
                planning_match = re.match(r"Planning stage (\d+)/5:\s*(\w+)", label)
                if label == "Classifying earliest affected Pod layer":
                    stage, percent = "impact_analysis", _phase_percent("impact_analysis", 0.55)
                elif planning_match:
                    stage = planning_match.group(2).lower()
                    percent = _phase_percent(stage)
                elif label == "Planning architecture":
                    stage, percent = "planning", _phase_percent("impact_analysis", 0.4)
                elif component_match:
                    current, total = int(component_match.group(1)), max(1, int(component_match.group(2)))
                    active_phase = _progress_phase(stage)
                    if active_phase not in {"models", "providers", "services"}:
                        active_phase = "services"
                    stage = active_phase
                    percent = _phase_percent(stage, (current - 1) / total)
                elif label.startswith("Composing pipeline:"):
                    stage, percent = "pipelines", _phase_percent("pipelines", 0.35)
                elif label == "Generating application entry point":
                    stage, percent = "interfaces", _phase_percent("interfaces", 0.6)
                elif label.startswith("Generating Interface artifact:"):
                    stage, percent = "interfaces", _phase_percent("interfaces", 0.45)
                elif label.startswith("Repairing current artifact:"):
                    stage, percent = "repair", _phase_percent("verification", 0.72)
                suffix = "starting…" if event_type == "llm_started" else f"{characters:,} characters received"
                if event_type == "llm_completed":
                    suffix = f"response complete · {characters:,} characters"
                if event_type in {"llm_started", "llm_completed"}:
                    marker = "●" if event_type == "llm_started" else "✓"
                    task["logs"].append(f"{marker} {label} · {suffix}")
                    if len(task["logs"]) > 500:
                        del task["logs"][:100]
                _apply_phase_progress(task, stage, percent)
                task["message"] = f"{label} · {suffix}"

    @staticmethod
    def _pod_task_snapshot(task: dict) -> dict:
        return {
            key: (list(value) if key == "logs" else value)
            for key, value in task.items()
            if key != "cancel_requested"
        }
