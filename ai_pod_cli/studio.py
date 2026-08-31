"""AIPod Studio desktop shell powered by pywebview and WebView2."""

from __future__ import annotations

import json
import io
import atexit
import os
import re
import threading
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

from ai_pod_cli.project_model import ProjectModelError, build_project_model
from ai_pod_cli.contracts import analyze_pipeline_contracts
from ai_pod_cli.run_store import get_run_trace, list_run_traces
from ai_pod_cli.run_store import write_run_trace
from ai_pod_cli.runner import PipelineRunner
from ai_pod_cli.config import init_config_if_not_exists, load_beans, register_route, save_config
from ai_pod_cli.studio_common import (
    PodCancelled as _PodCancelled, ProgressCapture as _ProgressCapture,
    StudioError, redirect_current_thread_stdout as _redirect_current_thread_stdout,
)
from ai_pod_cli.studio_pod import StudioPodService
from ai_pod_cli.studio_process import StudioProcessService
from ai_pod_cli.studio_project import StudioProjectService
from ai_pod_cli.studio_window import StudioWindowService


STUDIO_TITLE = "AIPod Studio"
_PROJECT_MARKERS = ("beans_config.json", "routes.toml", "config.toml")


class StudioApi(
    StudioProjectService, StudioPodService, StudioProcessService, StudioWindowService,
):
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
                invalid = [item for item in services if by_id[item].get("category") != "service"]
                if invalid:
                    raise StudioError(f"只有 Service 能进入 Pipeline：{', '.join(invalid)}")

                contract = analyze_pipeline_contracts(services, components)
                if not contract["valid"]:
                    issue = contract["issues"][0]
                    if issue["code"] == "semantic_field_drift":
                        raise StudioError(
                            f"疑似同义字段漂移：{issue['component']} 需要 '{issue['field']}'，"
                            f"但上游提供 '{issue['produced_field']}'。请统一字段名后再保存。"
                        )
                    if issue["code"] == "contract_schema_mismatch":
                        details = ", ".join(
                            f"{item['path']} ({item['produced']} -> {item['required']})"
                            for item in issue["schema_mismatches"]
                        )
                        raise StudioError(f"嵌套 Schema 不兼容：{details}")
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

    def _plan_ai_component(self, description: str) -> tuple[str, str]:
        from ai_pod_cli.client import call_llm

        plan = call_llm(
            "你是 AIPod 架构师。根据需求判断应创建 model（共享数据结构）、service（业务逻辑）还是 provider（基础设施），"
            "并给出简洁、合法、以大写字母开头的 Python 类名。只返回 JSON。",
            f"组件需求：{description}\n返回格式：{{\"name\":\"ClassName\",\"category\":\"model|service|provider\"}}",
            json_mode=True,
            temperature=0.1,
        )
        return str(plan.get("name", "")).strip(), str(plan.get("category", "")).strip()

    @staticmethod
    def _validate_component_identity(name: str, category: str) -> None:
        if not re.fullmatch(r"[A-Z][A-Za-z0-9_]*", name):
            raise StudioError("组件名必须是以大写字母开头的 Python 类名")
        if category not in {"model", "service", "provider"}:
            raise StudioError("类型必须是 model、service 或 provider")

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
