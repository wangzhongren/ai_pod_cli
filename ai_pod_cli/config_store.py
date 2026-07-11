"""ConfigStore — centralized TOML-based configuration entity."""

import os

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # Python < 3.11


class ConfigStore:
    """集中式配置组件，读取 config.toml 并提供 dot-notation 访问。

    用法（在其他组件中）：
        @inject
        def __init__(self, config_store: ConfigStore):
            db_path = config_store.get("database.sqlite_path", "data.db")
    """

    def __init__(self, config_path: str = "config.toml"):
        self._config_path = config_path
        self._data: dict = {}
        self._load()

    def _load(self):
        """从 TOML 文件加载配置。"""
        if os.path.exists(self._config_path):
            with open(self._config_path, "rb") as f:
                self._data = tomllib.load(f)
        else:
            self._data = {}

    def reload(self):
        """重新加载配置文件。"""
        self._load()

    def get(self, key: str, default=None):
        """通过 dot-notation 读取配置值。

        Args:
            key: 用点号分隔的键路径，如 "database.sqlite_path"
            default: 键不存在时的默认值

        Examples:
            config.get("database.sqlite_path")  → "data.db"
            config.get("server.port", 8080)     → 8080
        """
        parts = key.split(".")
        value = self._data
        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return default
            if value is None:
                return default
        return value

    def get_section(self, section: str) -> dict:
        """读取整个配置段。

        Args:
            section: 段名，如 "database"

        Returns:
            该段的所有键值对字典
        """
        return dict(self._data.get(section, {}))

    def sections(self) -> list[str]:
        """返回所有配置段名。"""
        return list(self._data.keys())

    def keys(self, section: str) -> list[str]:
        """返回指定段下的所有键名。"""
        return list(self._data.get(section, {}).keys())

    def all(self) -> dict:
        """返回完整配置字典。"""
        return dict(self._data)

    def __repr__(self):
        return f"ConfigStore(path={self._config_path}, sections={self.sections()})"
