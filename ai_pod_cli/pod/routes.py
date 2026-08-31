"""Read-only access to frozen Pipeline routes."""

import os


def load_routes_map() -> dict[str, str]:
    """读取 routes.toml，返回 {route_name: description} 映射。"""
    from ai_pod_cli.config import ROUTES_TOML

    routes_map = {}
    if os.path.exists(ROUTES_TOML):
        try:
            import tomlkit
            with open(ROUTES_TOML, "r", encoding="utf-8") as f:
                doc = tomlkit.load(f)
            for name, value in doc.items():
                if isinstance(value, dict):
                    desc = value.get("description", "")
                    routes_map[name] = str(desc) if desc else ""
                else:
                    routes_map[name] = ""
        except Exception:
            pass
    return routes_map
