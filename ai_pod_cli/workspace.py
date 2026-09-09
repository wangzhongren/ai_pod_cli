"""Shared file/shell tools with one write boundary per layer, in the real workspace."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from ai_pod_cli.source_codec import decode_source_artifact

LAYERS = ("models", "providers", "services", "pipelines", "interfaces")
SHARED = ("requirements.txt", "pyproject.toml", "config.toml", "README.md", ".venv", "venv", "tests/pod", "docs/pod")


def owned_paths(stage: str) -> list[str]:
    if stage not in LAYERS:
        return []
    source = f"modules/{stage}" if stage in LAYERS[:3] else stage
    return [source, f"tests/{stage}", f"docs/{stage}", *(["app.py"] if stage == "interfaces" else [])]


def path_owner(path: str) -> str | None:
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts or "\\" in path:
        return None
    for stage in LAYERS:
        if any(relative == Path(base) or Path(base) in relative.parents for base in owned_paths(stage)):
            return stage
    if any(relative == Path(base) or Path(base) in relative.parents for base in SHARED):
        return "pod"
    return None


def parse_action(raw: str) -> dict:
    """Source stays XML/CDATA; small tool arguments and finish manifests use JSON."""
    if not isinstance(raw, str):
        raise ValueError("Model action must be text")
    value = raw.strip()
    if value.startswith("<"):
        return {"tool": "write", **decode_source_artifact(value)}
    result = json.loads(value)
    if not isinstance(result, dict) or not isinstance(result.get("tool"), str):
        raise ValueError("Return exactly one tool action")
    if result["tool"] == "write":
        raise ValueError("File source must use XML/CDATA, not JSON")
    return result


class WorkspaceTools:
    def __init__(self, root, stage: str, paths: list[str] | None = None):
        self.root = Path(root).resolve()
        self.stage = stage
        self.paths = paths if paths is not None else (list(SHARED) if stage == "pod" else owned_paths(stage))
        if not self.paths or any(path_owner(path) != stage for path in self.paths):
            raise ValueError("Write grants must belong to the acting layer")
        self.scratch = self.root / ".aipod" / "work" / stage
        if self.scratch.resolve() != self.scratch:
            raise PermissionError("Workspace scratch may not be a symbolic link")
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.changed: set[str] = set()
        self.checks: list[dict] = []
        self.revision = 0
        if stage in LAYERS[:3]:
            modules = self.root / "modules"
            if modules.resolve() != modules:
                raise PermissionError("Project modules directory may not be a symbolic link")
            modules.mkdir(exist_ok=True)
            initializer = modules / "__init__.py"
            if not initializer.exists():
                initializer.write_text("", encoding="utf-8")
        for path in self.paths:
            target = self.resolve(path, write=True)
            # The controller creates owned directories before entering a read-only root.
            if self.directory_grant(path):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)

        if stage in {"providers", "services"} and f"modules/{stage}" in self.paths:
            for area in ("contracts", "impl", "public"):
                self.resolve(f"modules/{stage}/{area}", write=True).mkdir(exist_ok=True)

    def directory_grant(self, path: str) -> bool:
        known = {base for layer in LAYERS for base in owned_paths(layer) if base != "app.py"}
        known.update({".venv", "venv", "tests/pod", "docs/pod"})
        return path in known or (self.root / path).is_dir()

    def resolve(self, path: str, *, write=False) -> Path:
        if not isinstance(path, str) or not path or "\\" in path:
            raise PermissionError("Use a project-relative path")
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise PermissionError("Path escapes the project")
        if any(part in {".git", ".ssh", ".npmrc", ".pypirc"} or part == ".env" or part.startswith(".env.") for part in relative.parts):
            raise PermissionError("Credentials and Git internals are not agent files")
        if relative.parts[:1] == (".aipod",):
            raise PermissionError("Pod state is managed by the controller")
        lexical = self.root / relative
        target = lexical.resolve()
        if not target.is_relative_to(self.root) or target != lexical:
            raise PermissionError("Symbolic links cannot cross the file ownership boundary")
        if write:
            if not any(relative == Path(base) or self.directory_grant(base) and Path(base) in relative.parents for base in self.paths):
                raise PermissionError(f"{self.stage} cannot modify {path}; request_change must go through Pod")
            if target.is_file() and target.stat().st_nlink != 1:
                raise PermissionError("Cannot modify a hard-linked file")
        return target

    def files(self, path=".") -> list[str]:
        root = self.resolve(path)
        paths = [root] if root.is_file() else root.rglob("*")
        found = []
        for item in paths:
            relative = item.relative_to(self.root)
            if any(part in {".git", ".aipod", "node_modules", "venv", ".venv", "__pycache__"} for part in relative.parts):
                continue
            try:
                self.resolve(relative.as_posix())
            except PermissionError:
                continue
            if item.is_file():
                found.append(relative.as_posix())
            if len(found) >= 1000:
                break
        return sorted(found)

    def execute(self, action: dict) -> dict:
        tool, path = action["tool"], action.get("path", ".")
        if tool == "list":
            return {"files": self.files(path)}
        if tool == "read":
            target = self.resolve(path)
            if target.stat().st_size > 2000000:
                raise ValueError("Large file: use a bounded shell command to inspect the relevant part")
            content = target.read_bytes().decode("utf-8")
            offset, limit = int(action.get("offset", 0)), max(1, min(int(action.get("limit", 40000)), 40000))
            if offset < 0 or offset > len(content):
                raise ValueError("Invalid read offset")
            end = min(offset + limit, len(content))
            return {"path": path, "content": content[offset:end], "offset": offset,
                    "total_characters": len(content), "next_offset": end if end < len(content) else None}
        if tool == "search":
            text = action.get("text")
            if not isinstance(text, str) or not text:
                raise ValueError("search requires literal text")
            matches = []
            for name in self.files(path):
                try:
                    lines = self.resolve(name).read_text(encoding="utf-8").splitlines()
                except (UnicodeError, OSError):
                    continue
                matches.extend({"path": name, "line": i, "text": line[:500]}
                               for i, line in enumerate(lines, 1) if text in line)
                if len(matches) >= 100:
                    break
            return {"matches": matches[:100]}
        if tool in {"write", "delete"}:
            target = self.resolve(path, write=True)
            if tool == "write":
                content = action.get("content")
                if not isinstance(content, str):
                    raise ValueError("write requires text content")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content.encode("utf-8"))
            else:
                if not target.is_file():
                    raise ValueError("delete accepts a single existing file, not a directory")
                target.unlink()
            self.changed.add(path)
            self.revision += 1
            return {"path": path, "status": "written" if tool == "write" else "deleted"}
        if tool == "shell":
            return self.shell(action.get("command"), action.get("cwd", "."), action.get("timeout", 60))
        raise ValueError(f"Unknown tool: {tool}")

    def shell_command(self, command: str) -> list[str]:
        writable = [self.resolve(path, write=True) for path in self.paths] + [self.scratch]
        for root in writable:
            for file in root.rglob("*") if root.is_dir() else [root]:
                if file.is_file() and not file.is_symlink() and file.stat().st_nlink > 1:
                    raise PermissionError("Writable scope contains hard links; cannot guarantee upstream read-only access")
        if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file():
            grants = " ".join(f"(subpath {json.dumps(str(path))})" if path.is_dir()
                              else f"(literal {json.dumps(str(path))})" for path in writable)
            profile = "\n".join([
                "(version 1)", "(deny default)", "(allow file-read*)",
                "(allow process-exec)", "(allow process-fork)", "(allow sysctl-read)",
                "(allow mach-lookup)", "(allow network*)", "(allow signal (target self))",
                f'(allow file-write* {grants} (literal "/dev/null"))',
                # Linking an upstream inode into a writable directory must not permit writes.
                "(deny file-link)",
                "(deny file-write-unlink " + " ".join(f"(literal {json.dumps(str(path))})" for path in writable if path.is_dir()) + ")",
                '(deny file-read* (regex "/[.](env([.][^/]*)?|npmrc|pypirc)$"))',
            ])
            return ["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", command]
        if sys.platform.startswith("linux") and shutil.which("bwrap"):
            argv = [shutil.which("bwrap"), "--die-with-parent", "--unshare-pid", "--new-session", "--ro-bind", "/", "/"]
            for path in writable:
                if not path.exists():
                    # Create new shared root files with write before shell; do not
                    # materialize unrelated empty configuration merely to bind it.
                    continue
                argv.extend(["--bind", str(path), str(path)])
            return [*argv, "--proc", "/proc", "--dev", "/dev", "--", "/bin/sh", "-c", command]
        raise RuntimeError("Protected shell requires macOS sandbox-exec or Linux bubblewrap; an unrestricted shell is never used")

    def shell(self, command, cwd=".", timeout=60) -> dict:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("shell requires a command")
        timeout = max(1, min(int(timeout), 120))
        directory = self.resolve(cwd)
        if not directory.is_dir():
            raise ValueError("shell cwd must be a project directory")
        environment = {"PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", os.defpath), "HOME": str(self.scratch),
                       "TMPDIR": str(self.scratch), "TMP": str(self.scratch), "TEMP": str(self.scratch),
                       "PIP_CACHE_DIR": str(self.scratch / "pip"), "npm_config_cache": str(self.scratch / "npm"),
                       "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
                       "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
                       "AIPOD_BUILD_DIR": str(self.scratch / "build"), "PYTEST_ADDOPTS": "-p no:cacheprovider",
                       "AIPOD_AGENT_SHELL": "1", "AIPOD_PYTHON": sys.executable,
                       "AIPOD_DATABASE_URL": "sqlite:///" + str(self.scratch / "database.sqlite3")}
        started = time.monotonic()
        # A file captures output without unbounded memory use or children keeping pipes open.
        with tempfile.TemporaryFile(dir=self.scratch) as output:
            process = subprocess.Popen(self.shell_command(command), cwd=directory, env=environment,
                                       stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
            timed_out = False
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            output.seek(0, 2)
            output.seek(max(0, output.tell() - 16000))
            text = output.read().decode("utf-8", errors="replace")
        result = {"command": command, "exit_code": process.returncode, "timed_out": timed_out,
                  "output": text, "seconds": round(time.monotonic() - started, 3)}
        self.revision += 1
        self.checks.append({**result, "revision": self.revision})
        return result
