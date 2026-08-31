"""Shared Studio errors, cancellation, and thread-local output capture."""

import io
import sys
import threading
from contextlib import contextmanager


class StudioError(ValueError):
    """An error that can be safely presented in the Studio UI."""


class PodCancelled(BaseException):
    """Internal cooperative cancellation signal that bypasses generation retries."""


class ProgressCapture(io.StringIO):
    """Capture command output while forwarding complete lines to Studio."""

    def __init__(self, callback=None, cancelled=None):
        super().__init__()
        self._callback = callback
        self._cancelled = cancelled or (lambda: False)
        self._pending = ""

    def write(self, value):
        if self._cancelled():
            raise PodCancelled()
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
def redirect_current_thread_stdout(target):
    """Capture only this thread while preserving output from other threads."""
    previous = sys.stdout
    router = _ThreadOutputRouter(target, previous, threading.get_ident())
    sys.stdout = router
    try:
        yield target
    finally:
        if sys.stdout is router:
            sys.stdout = previous
