"""Stable SDK for AI-generated project Interface adapters."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PROJECT_MARKERS = ("beans_config.json", "routes.toml", "config.toml")


class InterfaceError(ValueError):
    """Raised when an Interface manifest or Adapter violates the runtime contract."""


def find_project_root(start: str | Path = ".") -> Path:
    candidate = Path(start).expanduser().resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for path in (candidate, *candidate.parents):
        if all((path / marker).is_file() for marker in PROJECT_MARKERS):
            return path
    raise InterfaceError(f"No AIPod project found from {candidate}")


def resolve_manifest(target: str, project_root: str | Path = ".") -> Path:
    root = find_project_root(project_root)
    raw = Path(str(target)).expanduser()
    if raw.is_absolute():
        candidate = raw.resolve()
    else:
        direct = (root / raw).resolve()
        named = (root / "interfaces" / str(target) / "interface.json").resolve()
        candidate = direct if direct.is_file() else named
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise InterfaceError("Interface manifest is outside the project") from error
    if not candidate.is_file():
        raise InterfaceError(f"Interface manifest not found: {candidate}")
    return candidate


def load_manifest(target: str, project_root: str | Path = ".") -> tuple[Path, dict]:
    path = resolve_manifest(target, project_root)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InterfaceError(f"Cannot read Interface manifest: {error}") from error
    if not isinstance(manifest, dict) or not manifest.get("name"):
        raise InterfaceError("Interface manifest requires a name")
    adapter = manifest.get("adapter")
    if not isinstance(adapter, dict) or not adapter.get("path") or not adapter.get("class_name"):
        raise InterfaceError("Interface manifest requires adapter.path and adapter.class_name")
    return path, manifest


@dataclass
class InterfaceContext:
    """The only project capability exposed to generated Interface adapters."""

    project_root: Path
    manifest: dict
    events: list[dict[str, Any]] = field(default_factory=list)
    _runner: Any = field(default=None, init=False, repr=False)
    _route_contracts: dict[str, dict] | None = field(default=None, init=False, repr=False)

    @contextmanager
    def activated(self):
        previous = Path.cwd()
        root_text = str(self.project_root)
        added = root_text not in sys.path
        if added:
            sys.path.insert(0, root_text)
        os.chdir(self.project_root)
        try:
            yield
        finally:
            os.chdir(previous)
            if added and root_text in sys.path:
                sys.path.remove(root_text)

    def runner(self):
        if self._runner is None:
            from ai_pod_cli.config import load_beans
            from ai_pod_cli.container import build_container
            from ai_pod_cli.runner import PipelineRunner

            with self.activated():
                self._runner = build_container(load_beans()).get(PipelineRunner)
        return self._runner

    def route_names(self) -> list[str]:
        """List frozen routes without importing or constructing project Beans."""
        from ai_pod_cli.runner import PipelineRunner

        with self.activated():
            return list(PipelineRunner().route_names())

    def route_contracts(self) -> dict[str, dict]:
        """Return public route inputs/outputs without exposing component internals."""
        if self._route_contracts is None:
            from ai_pod_cli.project_model import build_project_model

            with self.activated():
                model = build_project_model()
            self._route_contracts = {
                str(item.get("name")): {
                    "description": str(item.get("description", "")),
                    "inputs": dict(item.get("contract", {}).get("inputs", {})),
                    "outputs": dict(item.get("contract", {}).get("outputs", {})),
                }
                for item in model.get("pipelines", [])
                if item.get("name")
            }
        return dict(self._route_contracts)

    def route_contract(self, route: str) -> dict:
        """Return one route's public boundary Contract."""
        return dict(self.route_contracts().get(str(route), {}))

    def validate_route_params(self, route: str, params: dict | None = None) -> list[str]:
        """Validate Adapter-produced parameters without executing the Pipeline."""
        from ai_pod_cli.contracts import validate_contract_data

        contract = self.route_contract(route)
        with self.activated():
            return validate_contract_data(
                dict(params or {}), contract.get("inputs", {}), str(route),
            )

    def run_route(self, route: str, params: dict | None = None):
        with self.activated():
            return self.runner().run(str(route), dict(params or {}))

    def emit(self, event: str, payload: Any = None) -> None:
        self.events.append({"event": str(event), "payload": payload})


class InterfaceAdapter:
    """Base class implemented by one AI-generated project adapter."""

    def start(self, context: InterfaceContext, payload: Any = None):
        raise NotImplementedError

    def smoke(self, context: InterfaceContext) -> dict:
        declared = {
            str(route) for route in self.required_routes()
        }
        missing = sorted(declared - set(context.route_names()))
        payloads = self.smoke_payloads()
        missing_payloads = []
        contract_errors = {}
        for route in sorted(declared - set(missing)):
            inputs = context.route_contract(route).get("inputs", {})
            if inputs and route not in payloads:
                missing_payloads.append(route)
                continue
            errors = context.validate_route_params(route, payloads.get(route, {}))
            if errors:
                contract_errors[route] = errors
        return {
            "status": "passed" if not missing and not missing_payloads and not contract_errors else "failed",
            "required_routes": sorted(declared), "missing_routes": missing,
            "missing_smoke_payloads": missing_payloads,
            "contract_errors": contract_errors,
        }

    def required_routes(self) -> list[str]:
        return []

    def smoke_payloads(self) -> dict[str, dict]:
        """Return non-destructive sample params keyed by required route name."""
        return {}

    def stop(self, context: InterfaceContext) -> None:
        return None


def load_adapter(manifest: dict, project_root: str | Path = ".") -> InterfaceAdapter:
    root = find_project_root(project_root)
    spec = manifest.get("adapter", {})
    entry_path = spec.get("entry_path") or spec.get("path") or ""
    path = (root / str(entry_path)).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise InterfaceError("Adapter source is outside the project") from error
    if not path.is_file():
        raise InterfaceError(f"Adapter source not found: {path}")
    safe_name = re.sub(r"[^A-Za-z0-9_]", "_", str(manifest.get("name", "adapter")))
    package_name = f"aipod_interface_{safe_name}"
    package = types.ModuleType(package_name)
    package.__path__ = [str(path.parent)]
    package.__package__ = package_name
    sys.modules[package_name] = package
    module_name = f"{package_name}.adapter_entry"
    module_spec = importlib.util.spec_from_file_location(module_name, path)
    if module_spec is None or module_spec.loader is None:
        raise InterfaceError(f"Cannot load Adapter source: {path}")
    module = importlib.util.module_from_spec(module_spec)
    module.__package__ = package_name
    sys.modules[module_name] = module
    with InterfaceContext(root, manifest).activated():
        module_spec.loader.exec_module(module)
    class_name = str(spec.get("class_name", ""))
    adapter_class = getattr(module, class_name, None)
    if not isinstance(adapter_class, type) or not issubclass(adapter_class, InterfaceAdapter):
        raise InterfaceError(f"{class_name} must inherit InterfaceAdapter")
    return adapter_class()


def create_context(manifest: dict, project_root: str | Path = ".") -> InterfaceContext:
    return InterfaceContext(find_project_root(project_root), manifest)


def verify_adapter_candidate(
    project_root: str | Path, manifest: dict, sources: dict[str, str], timeout: int = 30,
) -> list[str]:
    """Import and smoke all Adapter source files in a disposable project copy."""
    root = find_project_root(project_root)
    adapter_spec = manifest.get("adapter", {})
    relative = Path(str(adapter_spec.get("entry_path") or adapter_spec.get("path") or ""))
    if relative.is_absolute() or ".." in relative.parts:
        return ["Adapter path must remain inside the project"]
    with tempfile.TemporaryDirectory(prefix="aipod_interface_adapter_") as tmp:
        copied = Path(tmp) / "project"
        shutil.copytree(root, copied, ignore=shutil.ignore_patterns("__pycache__", ".aipod"))
        for raw_path, code in sources.items():
            source_path = Path(str(raw_path))
            if source_path.is_absolute() or ".." in source_path.parts:
                return [f"Adapter source path is unsafe: {raw_path}"]
            target = copied / source_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(code, encoding="utf-8")
        target = copied / relative
        if not target.is_file():
            return [f"Adapter entry source was not generated: {relative.as_posix()}"]
        manifest_path = target.parent / "interface.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        helper = (
            "import json\n"
            "from ai_pod_cli.interface import create_context, load_adapter\n"
            f"manifest=json.load(open({str(manifest_path)!r}, encoding='utf-8'))\n"
            f"adapter=load_adapter(manifest, {str(copied)!r})\n"
            f"result=adapter.smoke(create_context(manifest, {str(copied)!r}))\n"
            "print(json.dumps(result, ensure_ascii=False, default=str))\n"
            "status=result.get('status') if isinstance(result, dict) else None\n"
            "raise SystemExit(1 if status == 'failed' else 0)\n"
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-c", helper], cwd=copied,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return [f"Adapter smoke exceeded {timeout} seconds"]
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip()
            return ["Adapter isolated smoke failed:\n" + detail[-5000:]]
    return []
