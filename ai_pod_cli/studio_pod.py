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
        match = re.search(r"\[(\d+)/(\d+)\]\s*生成\s+([^\s]+)", clean)
        if "verify_application" in clean:
            stage, percent, message = "verification", 96, "Running application verification."
        elif "repair_current_artifact" in clean:
            stage, percent, message = "repair", 97, "Repairing the current failing artifact."
        elif "拆解方案" in clean:
            stage, percent, message = "planned", 18, "Architecture planned."
        elif match:
            current, total, name = int(match.group(1)), max(1, int(match.group(2))), match.group(3)
            stage = "components"
            percent = 20 + int(50 * (current - 1) / total)
            message = f"Generating component {current}/{total}: {name}"
        elif "[生成 Pipeline]" in clean or "[Pipeline 复用]" in clean:
            stage, percent = "pipelines", 74
        elif "入口" in clean:
            stage, percent = "entrypoint", 90
        elif "完成" in clean or "验证" in clean:
            stage, percent = "validation", 96
        with self._pod_task_lock:
            task = self._pod_tasks.get(build_id)
            if task is None:
                return
            task["logs"].append(clean)
            if len(task["logs"]) > 500:
                del task["logs"][:100]
            if task["status"] == "running":
                task.update(
                    stage=stage or task["stage"],
                    percent=max(task["percent"], percent) if stage else task["percent"],
                    message=message[:300],
                )

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
                if label == "Planning architecture":
                    stage, percent = "planning", max(percent, 8)
                elif component_match:
                    current, total = int(component_match.group(1)), max(1, int(component_match.group(2)))
                    stage, percent = "components", 20 + int(50 * (current - 1) / total)
                elif label.startswith("Composing pipeline:"):
                    stage, percent = "pipelines", max(percent, 74)
                elif label == "Generating application entry point":
                    stage, percent = "entrypoint", max(percent, 90)
                elif label.startswith("Repairing current artifact:"):
                    stage, percent = "repair", max(percent, 97)
                suffix = "starting…" if event_type == "llm_started" else f"{characters:,} characters received"
                if event_type == "llm_completed":
                    suffix = f"response complete · {characters:,} characters"
                task.update(stage=stage, percent=percent, message=f"{label} · {suffix}")

    @staticmethod
    def _pod_task_snapshot(task: dict) -> dict:
        return {
            key: (list(value) if key == "logs" else value)
            for key, value in task.items()
            if key != "cancel_requested"
        }
