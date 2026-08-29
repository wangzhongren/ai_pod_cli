"""AIPod Studio desktop shell powered by pywebview and WebView2."""

from __future__ import annotations

import json
import io
import atexit
import os
import re
import shlex
import subprocess
import sys
import threading
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

from ai_pod_cli.project_model import ProjectModelError, build_project_model
from ai_pod_cli.contracts import analyze_pipeline_contracts
from ai_pod_cli.run_store import get_run_trace, list_run_traces
from ai_pod_cli.run_store import write_run_trace
from ai_pod_cli.runner import PipelineRunner
from ai_pod_cli.config import init_config_if_not_exists, load_beans, register_route, save_config


STUDIO_TITLE = "AIPod Studio"
_PROJECT_MARKERS = ("beans_config.json", "routes.toml", "config.toml")


class StudioError(ValueError):
    """An error that can be safely presented in the Studio UI."""


class _PodCancelled(BaseException):
    """Internal cooperative cancellation signal that bypasses generation retries."""


class _ProgressCapture(io.StringIO):
    """Capture command output while forwarding complete lines to Studio."""

    def __init__(self, callback=None, cancelled=None):
        super().__init__()
        self._callback = callback
        self._cancelled = cancelled or (lambda: False)
        self._pending = ""

    def write(self, value):
        if self._cancelled():
            raise _PodCancelled()
        if not isinstance(value, str):
            value = str(value)
        written = super().write(value)
        self._pending += value
        if len(self._pending) > 16_384 and "\n" not in self._pending:
            line, self._pending = self._pending[:16_384], ""
            if self._callback:
                self._callback(line + " … [truncated]")
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            if self._callback and line.strip():
                self._callback(line.rstrip("\r"))
        return written


class _ThreadOutputRouter:
    """Route writes from one worker thread while preserving all other stdout."""

    def __init__(self, target, fallback, thread_id: int):
        self._target = target
        self._fallback = fallback
        self._thread_id = thread_id

    def write(self, value):
        stream = self._target if threading.get_ident() == self._thread_id else self._fallback
        return stream.write(value)

    def flush(self):
        self._target.flush()
        return self._fallback.flush()

    def __getattr__(self, name):
        return getattr(self._fallback, name)


@contextmanager
def _redirect_current_thread_stdout(target):
    """Capture only this thread; contextlib.redirect_stdout is process-global."""
    previous = sys.stdout
    router = _ThreadOutputRouter(target, previous, threading.get_ident())
    sys.stdout = router
    try:
        yield target
    finally:
        if sys.stdout is router:
            sys.stdout = previous


class StudioApi:
    """Small, project-scoped API exposed to the webview renderer."""

    def __init__(self, project_root: str | Path):
        self._lock = threading.RLock()
        self._project_root = self._resolve_project(project_root)
        self._process = None
        self._process_output: list[str] = []
        self._process_lock = threading.RLock()
        self._pod_task_lock = threading.RLock()
        self._pod_tasks: dict[str, dict] = {}
        self._window_maximized = False
        atexit.register(self._terminate_on_exit)

    @property
    def project_root(self) -> Path:
        return self._project_root

    def inspect_project(self) -> dict:
        """Return the canonical AIPod project model plus recent run summaries."""
        try:
            with self._in_project():
                model = build_project_model()
                model["runs"] = list_run_traces()[:30]
                model["entrypoints"] = self._discover_entrypoints()
                model["interfaces"] = self._discover_interfaces(model["pipelines"])
                return {"ok": True, "project": model}
        except (ProjectModelError, OSError, ValueError) as error:
            return self._error(error)

    def open_project(self, path: str) -> dict:
        """Switch Studio to another explicitly selected AIPod project."""
        try:
            root = self._resolve_project(path)
            self.stop_program()
            with self._lock:
                self._project_root = root
            return self.inspect_project()
        except (OSError, StudioError, ValueError) as error:
            return self._error(error)

    def choose_project(self) -> dict:
        """Open the native folder picker and switch to the selected project."""
        try:
            import webview
            window = webview.active_window()
            if window is None:
                raise StudioError("无法获取当前 Studio 窗口")
            selected = window.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=str(self._project_root),
                allow_multiple=False,
            )
            if not selected:
                return {"ok": True, "cancelled": True}
            selected_root = Path(selected[0]).expanduser().resolve()
            if not selected_root.is_dir():
                raise StudioError(f"项目目录不存在：{selected_root}")
            if not any((selected_root / marker).exists() for marker in _PROJECT_MARKERS):
                return {"ok": True, "needs_initialization": True, "path": str(selected_root)}
            result = self.open_project(str(selected_root))
            if result.get("ok"):
                self._remember_project()
            return result
        except (OSError, StudioError, ValueError) as error:
            return self._error(error)

    def initialize_project(self, path: str) -> dict:
        """Initialize a selected ordinary folder and open it as an AIPod project."""
        try:
            root = Path(path).expanduser().resolve()
            if not root.is_dir():
                raise StudioError(f"目录不存在：{root}")
            with self._lock:
                previous = Path.cwd()
                os.chdir(root)
                try:
                    for directory in (Path("modules/providers"), Path("modules/services"), Path("pipelines")):
                        directory.mkdir(parents=True, exist_ok=True)
                    for init_file, description in (
                        (Path("modules/__init__.py"), "AIPod project components."),
                        (Path("modules/providers/__init__.py"), "Infrastructure provider components."),
                        (Path("modules/services/__init__.py"), "Business service components."),
                        (Path("pipelines/__init__.py"), "AIPod pipelines."),
                    ):
                        if not init_file.exists():
                            init_file.write_text(f'"""{description}"""\n', encoding="utf-8")
                    init_config_if_not_exists()
                    requirements = Path("requirements.txt")
                    if not requirements.exists():
                        requirements.write_text("# Project-specific Python dependencies\n", encoding="utf-8")
                    gitignore = Path(".gitignore")
                    if not gitignore.exists():
                        gitignore.write_text("__pycache__/\n*.pyc\n.aipod/\n", encoding="utf-8")
                finally:
                    os.chdir(previous)
            self.stop_program()
            with self._lock:
                self._project_root = root
            self._remember_project()
            result = self.inspect_project()
            if result.get("ok"):
                result["initialized"] = True
            return result
        except (OSError, StudioError, ValueError) as error:
            return self._error(error)

    def window_control(self, action: str) -> dict:
        """Handle custom frameless-window controls."""
        try:
            import webview
            window = webview.active_window()
            if window is None:
                raise StudioError("无法获取当前 Studio 窗口")
            if action == "minimize":
                window.minimize()
            elif action == "maximize":
                if self._window_maximized:
                    window.restore()
                else:
                    window.maximize()
                self._window_maximized = not self._window_maximized
            elif action == "close":
                self._terminate_on_exit()
                window.destroy()
            else:
                raise StudioError("不支持的窗口操作")
            return {"ok": True, "maximized": self._window_maximized}
        except (OSError, StudioError, ValueError) as error:
            return self._error(error)

    def _remember_project(self) -> None:
        from ai_pod_cli.commands.env import _save_global_config, get_global_env

        try:
            env = get_global_env()
            env["AIPOD_LAST_PROJECT"] = str(self._project_root)
            _save_global_config(env)
        except OSError:
            # Remembering the last folder is a convenience; a read-only user
            # config location must not make opening a valid project fail.
            return

    def read_component_source(self, component_id: str) -> dict:
        """Read a registered component source file without importing it."""
        try:
            with self._in_project():
                model = build_project_model()
            component = next(
                (item for item in model["components"] if item.get("id") == component_id),
                None,
            )
            if component is None:
                raise StudioError(f"未找到组件：{component_id}")
            class_path = component.get("class_path", "")
            if not class_path:
                raise StudioError("组件没有 class_path")
            relative = Path(*class_path.rsplit(".", 1)[0].split(".")).with_suffix(".py")
            if class_path.startswith("ai_pod_cli."):
                source_path = Path(__file__).parent / Path(*relative.parts[1:])
            else:
                source_path = self._safe_project_path(relative)
            if not source_path.exists():
                raise StudioError(f"源码文件不存在：{relative}")
            return {
                "ok": True,
                "component": component_id,
                "path": str(source_path),
                "source": source_path.read_text(encoding="utf-8"),
            }
        except (OSError, ProjectModelError, StudioError, ValueError) as error:
            return self._error(error)

    def read_run(self, run_id: str) -> dict:
        """Return one persisted run trace."""
        try:
            if not run_id.startswith("run_") or any(char in run_id for char in ("/", "\\", ".")):
                raise StudioError("无效的运行记录 ID")
            with self._in_project():
                trace = get_run_trace(run_id)
            if trace is None:
                raise StudioError(f"未找到运行记录：{run_id}")
            return {"ok": True, "run": trace}
        except (OSError, StudioError, ValueError) as error:
            return self._error(error)

    def create_component(self, spec: dict) -> dict:
        """Create a component from an AI description or import full metadata."""
        try:
            mode = str(spec.get("mode", "ai")).strip()
            description = str(spec.get("description", "")).strip()
            if not description:
                raise StudioError("请填写组件用途说明")
            with self._in_project():
                if mode == "ai":
                    name, category = self._plan_ai_component(description)
                    self._validate_component_identity(name, category)
                    return self._create_component_with_ai(name, category, description)
                if mode != "manual":
                    raise StudioError("创建模式必须是 ai 或 manual")
                return self._import_component(spec, description)
        except (OSError, StudioError, ValueError, KeyError, json.JSONDecodeError) as error:
            return self._error(error)

    def compose_pipeline(self, spec: dict) -> dict:
        """Create and register a deterministic sequential Pipeline from services."""
        try:
            name = str(spec.get("name", "")).strip()
            description = str(spec.get("description", "")).strip()
            services = spec.get("services", [])
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,59}", name):
                raise StudioError("路由名必须以字母开头，只能包含字母、数字、下划线和连字符")
            if not isinstance(services, list) or not services:
                raise StudioError("请至少选择一个 Service")
            services = [str(item) for item in services]

            with self._in_project():
                config = load_beans()
                components = config.get("beans", [])
                by_id = {item.get("id"): item for item in components}
                unknown = [item for item in services if item not in by_id]
                if unknown:
                    raise StudioError(f"组件不存在：{', '.join(unknown)}")
                providers = [item for item in services if by_id[item].get("category") == "provider"]
                if providers:
                    raise StudioError(f"Provider 不能进入 Pipeline：{', '.join(providers)}")

                contract = analyze_pipeline_contracts(services, components)
                if not contract["valid"]:
                    issue = contract["issues"][0]
                    if issue["code"] == "semantic_field_drift":
                        raise StudioError(
                            f"疑似同义字段漂移：{issue['component']} 需要 '{issue['field']}'，"
                            f"但上游提供 '{issue['produced_field']}'。请统一字段名后再保存。"
                        )
                    raise StudioError(
                        f"契约不兼容：{issue['component']}.{issue['field']} "
                        f"需要 {issue['required']}，上游提供 {issue['produced']}"
                    )

                imports = []
                classes = []
                for service_id in services:
                    module, class_name = by_id[service_id]["class_path"].rsplit(".", 1)
                    line = f"from {module} import {class_name}"
                    if line not in imports:
                        imports.append(line)
                    classes.append(class_name)
                chain = " | ".join(f"S({class_name})" for class_name in classes)
                code = (
                    '"""Pipeline composed visually by AIPod Studio."""\n\n'
                    "from ai_pod_cli.config import load_beans\n"
                    "from ai_pod_cli.container import Pod, build_container\n"
                    "from ai_pod_cli.context import PipelineContext\n"
                    + "\n".join(imports)
                    + "\n\n\ndef run(ctx: PipelineContext) -> dict:\n"
                    "    container = build_container(load_beans())\n"
                    "    S = Pod(container)\n"
                    f"    ({chain}).execute_all(ctx)\n"
                    "    return ctx.summary()\n"
                )
                pipeline_dir = self._safe_project_path(Path("pipelines"))
                pipeline_dir.mkdir(parents=True, exist_ok=True)
                pipeline_path = pipeline_dir / f"{name}.py"
                pipeline_path.write_text(code, encoding="utf-8")
                relative_path = f"pipelines/{name}.py"
                register_route(name, relative_path, description or f"Visual pipeline: {' → '.join(services)}")
                project = build_project_model()
                project["runs"] = list_run_traces()[:30]
                project["entrypoints"] = self._discover_entrypoints()
                project["interfaces"] = self._discover_interfaces(project["pipelines"])
            return {"ok": True, "pipeline": name, "contract": contract, "project": project}
        except (OSError, StudioError, ValueError, KeyError) as error:
            return self._error(error)

    def get_settings(self) -> dict:
        """Return global model settings without exposing the API key."""
        try:
            from ai_pod_cli.commands.env import _global_config_path, get_global_env

            env = get_global_env()
            api_key = str(env.get("OPENAI_API_KEY", ""))
            return {
                "ok": True,
                "settings": {
                    "api_key_configured": bool(api_key),
                    "api_key_masked": _mask_secret(api_key),
                    "base_url": str(env.get("OPENAI_BASE_URL", "https://api.openai.com/v1")),
                    "model": str(env.get("OPENAI_MODEL", "deepseek-chat")),
                    "config_path": _global_config_path(),
                },
            }
        except (OSError, ValueError) as error:
            return self._error(error)

    def save_settings(self, settings: dict) -> dict:
        """Persist global model settings and refresh this process environment."""
        try:
            from ai_pod_cli.commands.env import _save_global_config, get_global_env
            from ai_pod_cli import client

            base_url = str(settings.get("base_url", "")).strip().rstrip("/")
            model = str(settings.get("model", "")).strip()
            api_key = str(settings.get("api_key", "")).strip()
            if not re.match(r"^https?://", base_url):
                raise StudioError("Base URL 必须以 http:// 或 https:// 开头")
            if not model:
                raise StudioError("请填写模型名称")
            env = get_global_env()
            env["OPENAI_BASE_URL"] = base_url
            env["OPENAI_MODEL"] = model
            if api_key:
                env["OPENAI_API_KEY"] = api_key
            if settings.get("clear_api_key"):
                env.pop("OPENAI_API_KEY", None)
            _save_global_config(env)
            for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
                if key in env:
                    os.environ[key] = str(env[key])
                else:
                    os.environ.pop(key, None)
            client._client = None
            client._model = None
            return self.get_settings()
        except (OSError, StudioError, ValueError) as error:
            return self._error(error)

    def start_pod_build(self, description: str) -> dict:
        """Start one Pod build in the background and return its task id."""
        description = str(description).strip()
        if not description:
            return self._error(StudioError("请描述你想构建的程序"))
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
                "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None,
            }
            self._pod_tasks[build_id] = task
        threading.Thread(
            target=self._run_pod_task,
            args=(build_id, description),
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

    def build_pod(self, description: str, _progress=None, _cancelled=None) -> dict:
        """Run the canonical Pod workflow from one natural-language requirement."""
        try:
            description = str(description).strip()
            if not description:
                raise StudioError("请描述你想构建的程序")
            with self._in_project():
                before = build_project_model()
                before_entries = set(self._discover_entrypoints())
                from ai_pod_cli.commands.pod import handle_pod
                output = _ProgressCapture(_progress, _cancelled)
                args = SimpleNamespace(
                    desc=description, file="", yes=True, json=True,
                    auto_repair=True,
                    progress_callback=getattr(_progress, "event", None),
                )
                with _redirect_current_thread_stdout(output):
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
                if not added_components and not added_pipelines and not reused_components and not reused_pipelines:
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

    def _run_pod_task(self, build_id: str, description: str) -> None:
        def cancelled():
            with self._pod_task_lock:
                return bool(self._pod_tasks[build_id]["cancel_requested"])

        def progress(line: str):
            self._record_pod_progress(build_id, line)

        def event(payload: dict):
            if cancelled():
                raise _PodCancelled()
            self._record_pod_event(build_id, payload)

        progress.event = event

        try:
            result = self.build_pod(description, progress, cancelled)
            with self._pod_task_lock:
                task = self._pod_tasks[build_id]
                if task["cancel_requested"]:
                    task.update(status="cancelled", stage="cancelled", message="Pod build cancelled.")
                elif result.get("ok"):
                    task.update(status="completed", stage="completed", percent=100, message="Pod build completed.", result=result)
                else:
                    task.update(status="failed", stage="failed", message=result.get("error", {}).get("message", "Pod build failed."), error=result.get("error"))
                task["finished_at"] = datetime.now(timezone.utc).isoformat()
        except _PodCancelled:
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
        if "拆解方案" in clean:
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

    def generate_entrypoint(self, description: str) -> dict:
        """Generate a project entry point using the canonical entry generator."""
        try:
            description = str(description).strip()
            if not description:
                raise StudioError("请描述程序入口的使用方式")
            with self._in_project():
                before = set(self._discover_entrypoints())
                from ai_pod_cli.entry_generator import generate_entry
                output = io.StringIO()
                with redirect_stdout(output):
                    result = generate_entry(description)
                if result is None:
                    diagnostics = [line for line in output.getvalue().splitlines() if line.strip()]
                    raise StudioError(diagnostics[-1] if diagnostics else "AI 未能生成入口文件")
                entry_file, dependencies = result
                entries = self._discover_entrypoints()
                if entry_file not in entries:
                    raise StudioError(f"入口文件未写入项目：{entry_file}")
                created = entry_file not in before
            project = self.inspect_project()["project"]
            return {
                "ok": True, "entrypoint": entry_file, "created": created,
                "dependencies": dependencies, "project": project,
                "diagnostics": output.getvalue().splitlines(),
            }
        except (OSError, StudioError, ValueError) as error:
            return self._error(error)

    def run_pipeline(self, route: str, params) -> dict:
        """Execute a registered route and persist the same trace as `aipod run`."""
        try:
            route = str(route).strip()
            if isinstance(params, str):
                params = json.loads(params or "{}")
            if not isinstance(params, dict):
                raise StudioError("运行参数必须是 JSON 对象")
            started = perf_counter()
            started_at = datetime.now(timezone.utc).isoformat()
            with self._in_project():
                try:
                    result, _context = PipelineRunner().run_with_context(route, params)
                    trace = write_run_trace(route, params, result, None, (perf_counter() - started) * 1000, started_at)
                except Exception as error:
                    context = getattr(error, "aipod_context", None)
                    partial = context.summary() if context is not None else None
                    trace = write_run_trace(route, params, partial, error, (perf_counter() - started) * 1000, started_at)
            project = self.inspect_project()["project"]
            return {"ok": trace["status"] == "success", "trace": trace, "project": project,
                    **({"error": trace["error"]} if trace["error"] else {})}
        except (OSError, StudioError, ValueError, json.JSONDecodeError) as error:
            return self._error(error)

    def start_program_request(self, request: dict | None = None) -> dict:
        """Stable one-object bridge for starting an interface from WebView2."""
        request = request if isinstance(request, dict) else {}
        return self.start_program(
            str(request.get("entry", "")),
            str(request.get("arguments", "")),
        )

    def start_program(self, entry: str = "", arguments: str = "") -> dict:
        """Start a project Python entry point without invoking a shell."""
        try:
            if not str(entry).strip():
                entries = self._discover_entrypoints()
                if not entries:
                    raise StudioError("当前项目没有可运行的 Python 入口文件")
                entry = entries[0]
            entry_path = self._safe_project_path(Path(str(entry)))
            if entry_path.suffix.lower() != ".py" or not entry_path.is_file():
                raise StudioError("请选择当前项目中的 Python 入口文件")
            args = shlex.split(str(arguments))
            with self._process_lock:
                if self._process is not None and self._process.poll() is None:
                    raise StudioError("程序正在运行，请先停止当前进程")
                self._process_output = [f"> {sys.executable} -u {entry_path.name} {' '.join(args)}".rstrip()]
                child_env = os.environ.copy()
                child_env["PYTHONUTF8"] = "1"
                child_env["PYTHONUNBUFFERED"] = "1"
                self._process = subprocess.Popen(
                    [sys.executable, "-u", str(entry_path), *args],
                    cwd=str(self._project_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=child_env,
                )
                threading.Thread(target=self._collect_process_output, daemon=True).start()
                return {"ok": True, "pid": self._process.pid, "command": self._process_output[0]}
        except (OSError, StudioError, ValueError) as error:
            return self._error(error)

    def program_status(self) -> dict:
        with self._process_lock:
            process = self._process
            running = process is not None and process.poll() is None
            return {
                "ok": True, "running": running,
                "pid": process.pid if process else None,
                "exit_code": None if running or process is None else process.returncode,
                "output": list(self._process_output),
            }

    def stop_program(self) -> dict:
        try:
            with self._process_lock:
                if self._process is None or self._process.poll() is not None:
                    return self.program_status()
                self._process.terminate()
                try:
                    self._process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                self._process_output.append("[AIPod Studio] Process stopped.")
                return self.program_status()
        except OSError as error:
            return self._error(error)

    def _collect_process_output(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            with self._process_lock:
                self._process_output.append(line.rstrip("\r\n"))
                if len(self._process_output) > 2000:
                    del self._process_output[:500]
        process.wait()
        with self._process_lock:
            self._process_output.append(f"[AIPod Studio] Process exited with code {process.returncode}.")

    def _terminate_on_exit(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def _discover_entrypoints(self) -> list[str]:
        preferred = ("main.py", "app.py", "server.py", "cli.py", "consumer.py", "scheduler.py")
        found = [name for name in preferred if (self._project_root / name).is_file()]
        for path in sorted(self._project_root.glob("*.py")):
            if path.name not in found and not path.name.startswith(("setup", "test_")):
                found.append(path.name)
        return found

    def _discover_interfaces(self, pipelines: list[dict]) -> list[dict]:
        """Describe project entry files as user-facing interfaces."""
        route_names = [item["name"] for item in pipelines]
        interfaces = []
        for name in self._discover_entrypoints():
            path = self._project_root / name
            try:
                source = path.read_text(encoding="utf-8")
            except OSError:
                continue
            lowered = source.lower()
            if "pywebview" in lowered or "import webview" in lowered:
                kind = "desktop"
            elif "http.server" in lowered and ("text/html" in lowered or "<!doctype html" in lowered):
                kind = "web"
            elif "fastapi" in lowered or "flask" in lowered or "http.server" in lowered:
                kind = "api"
            elif "argparse" in lowered or "click" in lowered or "typer" in lowered:
                kind = "cli"
            else:
                kind = "python"
            explicit = []
            for route in route_names:
                if re.search(rf"[\"']{re.escape(route)}[\"']", source):
                    explicit.append(route)
            dynamic_dispatch = bool(re.search(r"\.run\(\s*[A-Za-z_]\w*\s*,", source))
            interfaces.append({
                "name": name, "kind": kind, "entrypoint": name,
                "routes": route_names if dynamic_dispatch else explicit,
                "command": f"python -u {name}",
            })
        return interfaces

    def _plan_ai_component(self, description: str) -> tuple[str, str]:
        from ai_pod_cli.client import call_llm

        plan = call_llm(
            "你是 AIPod 组件架构师。根据需求判断应创建 service（业务逻辑）还是 provider（基础设施），"
            "并给出简洁、合法、以大写字母开头的 Python 类名。只返回 JSON。",
            f"组件需求：{description}\n返回格式：{{\"name\":\"ClassName\",\"category\":\"service|provider\"}}",
            json_mode=True,
            temperature=0.1,
        )
        return str(plan.get("name", "")).strip(), str(plan.get("category", "")).strip()

    @staticmethod
    def _validate_component_identity(name: str, category: str) -> None:
        if not re.fullmatch(r"[A-Z][A-Za-z0-9_]*", name):
            raise StudioError("组件名必须是以大写字母开头的 Python 类名")
        if category not in {"service", "provider"}:
            raise StudioError("组件类型必须是 service 或 provider")

    def _import_component(self, spec: dict, description: str) -> dict:
        name = str(spec.get("name", "")).strip()
        category = str(spec.get("category", "")).strip()
        class_path = str(spec.get("class_path", "")).strip()
        self._validate_component_identity(name, category)
        if not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)+", class_path):
            raise StudioError("请填写完整类路径，例如 modules.services.worker.Worker")
        dependencies = _string_list(spec.get("dependencies", []))
        inputs = _json_object(spec.get("inputs", {}), "inputs")
        outputs = _json_object(spec.get("outputs", {}), "outputs")
        methods = _json_object(spec.get("methods", {}), "methods")
        module_path = Path(*class_path.rsplit(".", 1)[0].split(".")).with_suffix(".py")
        source_path = self._safe_project_path(module_path)
        if not source_path.exists():
            raise StudioError(f"类路径对应的源码不存在：{module_path}")

        beans = load_beans()
        if any(bean.get("id") == name for bean in beans.get("beans", [])):
            raise StudioError(f"组件已存在：{name}")
        known_ids = {bean.get("id") for bean in beans.get("beans", [])}
        missing = [item for item in dependencies if item not in known_ids]
        if missing:
            raise StudioError(f"依赖尚未注册：{', '.join(missing)}")
        beans["beans"].append({
            "id": name, "category": category, "type": "human_added",
            "class_path": class_path, "file": source_path.name,
            "dependencies": dependencies, "inputs": inputs, "outputs": outputs,
            "methods": methods, "description": description,
        })
        save_config(beans)
        return {"ok": True, "component": name, "mode": "manual", "project": self.inspect_project()["project"]}

    def _assert_component_available(self, name: str) -> None:
        """Ensure an AI-planned component does not replace an existing bean."""
        beans = load_beans()
        if any(bean.get("id") == name for bean in beans.get("beans", [])):
            raise StudioError(f"组件已存在：{name}")

    def _create_component_with_ai(self, name: str, category: str, description: str) -> dict:
        from ai_pod_cli.commands.create import handle_create

        self._assert_component_available(name)
        output = io.StringIO()
        args = SimpleNamespace(name=name, category=category, desc=description, json=True)
        with redirect_stdout(output):
            handle_create(args)
        beans = load_beans()
        if not any(bean.get("id") == name for bean in beans.get("beans", [])):
            diagnostics = [line for line in output.getvalue().splitlines() if line.strip()]
            message = diagnostics[-1] if diagnostics else "AI 未生成组件，请检查模型配置"
            raise StudioError(message)
        return {
            "ok": True, "component": name, "category": category, "mode": "ai",
            "diagnostics": output.getvalue().splitlines(),
            "project": self.inspect_project()["project"],
        }

    @contextmanager
    def _in_project(self):
        # The existing project model is cwd-based. Serialize cwd changes until
        # it can accept an explicit project root throughout the core API.
        with self._lock:
            previous = Path.cwd()
            os.chdir(self._project_root)
            try:
                yield
            finally:
                os.chdir(previous)

    def _safe_project_path(self, relative: Path) -> Path:
        candidate = (self._project_root / relative).resolve()
        try:
            candidate.relative_to(self._project_root)
        except ValueError as error:
            raise StudioError("文件路径超出当前项目目录") from error
        return candidate

    @staticmethod
    def _resolve_project(project_root: str | Path) -> Path:
        root = Path(project_root).expanduser().resolve()
        if not root.is_dir():
            raise StudioError(f"项目目录不存在：{root}")
        if not any((root / marker).exists() for marker in _PROJECT_MARKERS):
            raise StudioError(f"不是 AIPod 项目：{root}（未找到项目配置）")
        return root

    @staticmethod
    def _error(error: Exception) -> dict:
        return {"ok": False, "error": {"type": type(error).__name__, "message": str(error)}}


def studio_asset_path() -> Path:
    return Path(__file__).with_name("studio_assets") / "index.html"


def launch_studio(project_root: str | Path = ".", *, debug: bool = False) -> None:
    """Launch the native AIPod Studio window."""
    try:
        import webview
    except ImportError as error:
        raise StudioError(
            "AIPod Studio 需要 pywebview。请运行：pip install 'AIPodCli[studio]'"
        ) from error

    api = StudioApi(_startup_project(project_root))
    asset = studio_asset_path()
    if not asset.exists():
        raise StudioError(f"Studio 前端资源不存在：{asset}")

    # Only direct hits on explicitly marked regions initiate window dragging;
    # interactive controls nested in the title bar keep receiving clicks.
    webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True
    webview.create_window(
        STUDIO_TITLE,
        asset.resolve().as_uri(),
        js_api=api,
        width=1440,
        height=900,
        min_size=(1000, 650),
        background_color="#0b0f19",
        text_select=True,
        frameless=True,
        easy_drag=False,
    )
    webview.start(gui="edgechromium" if os.name == "nt" else None, debug=debug)


def project_model_json(project_root: str | Path) -> str:
    """Convenience helper used by packaging and smoke tests."""
    return json.dumps(StudioApi(project_root).inspect_project(), ensure_ascii=False)


def _string_list(value) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [item.strip() for item in value if item.strip()]
    raise StudioError("dependencies 必须是逗号分隔的组件 ID")


def _json_object(value, field: str) -> dict:
    if isinstance(value, str):
        value = json.loads(value or "{}")
    if not isinstance(value, dict):
        raise StudioError(f"{field} 必须是 JSON 对象")
    return value


def _mask_secret(value: str) -> str:
    if not value:
        return "Not configured"
    if len(value) <= 8:
        return "••••••••"
    return f"{value[:4]}••••{value[-4:]}"


def _startup_project(project_root: str | Path) -> str | Path:
    """Use the last opened project when Studio starts without a project cwd."""
    candidate = Path(project_root).expanduser().resolve()
    if any((candidate / marker).exists() for marker in _PROJECT_MARKERS):
        return candidate
    if str(project_root) in {".", ""}:
        try:
            from ai_pod_cli.commands.env import get_global_env
            remembered = get_global_env().get("AIPOD_LAST_PROJECT", "")
            if remembered and Path(remembered).is_dir():
                return remembered
        except (OSError, ValueError):
            pass
    return candidate
