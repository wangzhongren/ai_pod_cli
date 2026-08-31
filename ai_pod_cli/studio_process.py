"""Studio subprocess lifecycle service."""

import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path

from ai_pod_cli.studio_common import StudioError


class StudioProcessService:
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
