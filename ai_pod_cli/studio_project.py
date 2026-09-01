"""Studio project selection, inspection, initialization, and path safety."""

import os
import re
from contextlib import contextmanager
from pathlib import Path

from ai_pod_cli.config import init_config_if_not_exists
from ai_pod_cli.pod.state import load_current_plan
from ai_pod_cli.project_model import ProjectModelError, build_project_model
from ai_pod_cli.run_store import get_run_trace, list_run_traces
from ai_pod_cli.studio_common import StudioError


PROJECT_MARKERS = ("beans_config.json", "routes.toml", "config.toml")


class StudioProjectService:
    @property
    def project_root(self) -> Path:
        return self._project_root

    def inspect_project(self) -> dict:
        """Return the canonical AIPod project model plus recent run summaries."""
        try:
            with self._in_project():
                model = build_project_model()
                model["project_root"] = str(self._project_root)
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
            selected_root = Path(selected[0]).expanduser().absolute()
            if not selected_root.is_dir():
                raise StudioError(f"项目目录不存在：{selected_root}")
            if not any((selected_root / marker).exists() for marker in PROJECT_MARKERS):
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
            root = Path(path).expanduser().absolute()
            if not root.is_dir():
                raise StudioError(f"目录不存在：{root}")
            with self._lock:
                previous = Path.cwd()
                os.chdir(root)
                try:
                    for directory in (
                        Path("modules/models"), Path("modules/providers"),
                        Path("modules/services"), Path("pipelines"), Path("interfaces"),
                    ):
                        directory.mkdir(parents=True, exist_ok=True)
                    for init_file, description in (
                        (Path("modules/__init__.py"), "AIPod project components."),
                        (Path("modules/providers/__init__.py"), "Infrastructure provider components."),
                        (Path("modules/services/__init__.py"), "Business service components."),
                        (Path("modules/models/__init__.py"), "Shared typed data models."),
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

    def _discover_entrypoints(self) -> list[str]:
        preferred = ("main.py", "app.py", "server.py", "cli.py", "consumer.py", "scheduler.py")
        found = [name for name in preferred if (self._project_root / name).is_file()]
        for path in sorted(self._project_root.glob("*.py")):
            if path.name not in found and not path.name.startswith(("setup", "test_")):
                found.append(path.name)
        state = load_current_plan()
        plan = state.get("stages", {}).get("interfaces", {}).get("plan", {}) if state else {}
        for interface in plan.get("interfaces", []) if isinstance(plan, dict) else []:
            for artifact in interface.get("artifacts", []) if isinstance(interface, dict) else []:
                path = str(artifact.get("path", "")) if isinstance(artifact, dict) else ""
                if isinstance(artifact, dict) and artifact.get("role") == "runtime" and path and (self._project_root / path).is_file():
                    if path not in found:
                        found.append(path)
        return found

    def _discover_interfaces(self, pipelines: list[dict]) -> list[dict]:
        """Describe project entry files as user-facing interfaces."""
        route_names = [item["name"] for item in pipelines]
        state = load_current_plan()
        interface_plan = (
            state.get("stages", {}).get("interfaces", {}).get("plan", {})
            if state else {}
        )
        declared_items = [
            item for item in interface_plan.get("interfaces", [])
            if isinstance(item, dict) and item.get("name")
        ] if isinstance(interface_plan, dict) else []
        interfaces = []
        declared_runtime_paths = set()
        for metadata in declared_items:
            artifacts = metadata.get("artifacts", [])
            runtime = next((
                str(item.get("path")) for item in artifacts
                if isinstance(item, dict) and item.get("role") == "runtime" and item.get("path")
            ), "")
            if runtime:
                declared_runtime_paths.add(runtime)
            source = ""
            if runtime:
                try:
                    source = (self._project_root / runtime).read_text(encoding="utf-8")
                except OSError:
                    pass
            explicit = [
                route for route in route_names
                if re.search(rf"[\"']{re.escape(route)}[\"']", source)
            ]
            lifecycle = metadata.get("lifecycle", {})
            run_command = lifecycle.get("run", []) if isinstance(lifecycle, dict) else []
            interfaces.append({
                "name": str(metadata.get("name")),
                "kind": str(metadata.get("kind", "python")),
                "platform": str(metadata.get("platform", "cross-platform")),
                "entrypoint": runtime,
                "routes": explicit,
                "command": " ".join(str(item) for item in run_command),
                "artifacts": artifacts,
                "lifecycle": lifecycle,
                "permissions": metadata.get("permissions", []),
                "support": metadata.get("support", {}),
                "verify": metadata.get("verify", []),
            })

        for name in self._discover_entrypoints():
            if name in declared_runtime_paths:
                continue
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
                "artifacts": [{"path": name, "role": "runtime", "format": "python"}],
                "lifecycle": {"run": ["python", "-u", name], "install": [], "uninstall": []},
                "permissions": [], "support": {"level": "legacy", "manual_steps": []},
                "verify": [],
            })
        return interfaces

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
            candidate.relative_to(self._project_root.resolve())
        except ValueError as error:
            raise StudioError("文件路径超出当前项目目录") from error
        return candidate

    @staticmethod
    def _resolve_project(project_root: str | Path) -> Path:
        root = Path(project_root).expanduser().resolve()
        if not root.is_dir():
            raise StudioError(f"项目目录不存在：{root}")
        if not any((root / marker).exists() for marker in PROJECT_MARKERS):
            raise StudioError(f"不是 AIPod 项目：{root}（未找到项目配置）")
        return root

    @staticmethod
    def _error(error: Exception) -> dict:
        return {"ok": False, "error": {"type": type(error).__name__, "message": str(error)}}
