"""Studio desktop-window control service."""

from ai_pod_cli.studio_common import StudioError


class StudioWindowService:
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
