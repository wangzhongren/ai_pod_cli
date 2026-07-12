"""`config` command — manage global configuration at ~/.aipod/config.toml."""

import os
import platform

import tomlkit


def _global_config_dir() -> str:
    """Get the global config directory path."""
    if platform.system() == "Windows":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        return os.path.join(base, "aipod")
    else:
        return os.path.join(os.path.expanduser("~"), ".aipod")


def _global_config_path() -> str:
    """Get the global config file path."""
    return os.path.join(_global_config_dir(), "config.toml")


def _load_global_config() -> dict:
    """Load global config from ~/.aipod/config.toml."""
    path = _global_config_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return dict(tomlkit.load(f))
    return {}


def _save_global_config(data: dict):
    """Save global config to ~/.aipod/config.toml."""
    config_dir = _global_config_dir()
    os.makedirs(config_dir, exist_ok=True)

    path = _global_config_path()

    # Preserve existing comments if file exists
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            doc = tomlkit.load(f)
    else:
        doc = tomlkit.document()
        doc.add(tomlkit.comment("AIPod global configuration"))
        doc.add(tomlkit.comment("Shared across all projects"))
        doc.add(tomlkit.nl())

    # Write data into [env] section
    if "env" not in doc:
        doc.add("env", tomlkit.table())

    for key, value in data.items():
        doc["env"][key] = value

    with open(path, "w", encoding="utf-8") as f:
        tomlkit.dump(doc, f)


def get_global_env() -> dict:
    """Get global env settings. Used by other commands to auto-populate .env."""
    config = _load_global_config()
    return dict(config.get("env", {}))


def handle_config(args):
    """【config 命令】管理全局配置"""
    action = args.action

    if action == "list" or action is None:
        _config_list()
    elif action == "set":
        _config_set(args.key, args.value)
    elif action == "get":
        _config_get(args.key)
    elif action == "remove":
        _config_remove(args.key)
    elif action == "path":
        print(f"📁 {_global_config_path()}")
    else:
        print(f"❌ 未知操作: {action}")
        print(f"   可用: list, set, get, remove, path")


def _config_list():
    """Show all global config."""
    env = get_global_env()
    config_path = _global_config_path()

    if not env:
        print(f"📋 全局配置为空 ({config_path})\n")
        print(f"   设置大模型配置（一次设置，所有项目共用）:")
        print(f"   aipod config set OPENAI_API_KEY sk-your-key")
        print(f"   aipod config set OPENAI_BASE_URL https://api.openai.com/v1")
        print(f"   aipod config set OPENAI_MODEL deepseek-chat")
        return

    print(f"📋 全局配置 ({config_path}):\n")
    for key, value in env.items():
        display_value = _mask_value(key, value)
        print(f"   {key} = {display_value}")


def _config_set(key: str, value: str):
    """Set a global config value."""
    if not key or not value:
        print("❌ 用法: aipod config set KEY VALUE")
        return

    key = key.upper()
    env = get_global_env()
    env[key] = value
    _save_global_config(env)

    display_value = _mask_value(key, value)
    print(f"✅ {key} = {display_value}")
    print(f"   已保存到全局配置，所有新项目自动继承。")


def _config_get(key: str):
    """Get a global config value."""
    if not key:
        print("❌ 用法: aipod config get KEY")
        return

    key = key.upper()
    env = get_global_env()
    if key in env:
        display_value = _mask_value(key, env[key])
        print(f"{key} = {display_value}")
    else:
        print(f"❌ {key} 未配置。")


def _config_remove(key: str):
    """Remove a global config value."""
    if not key:
        print("❌ 用法: aipod config remove KEY")
        return

    key = key.upper()
    env = get_global_env()
    if key in env:
        del env[key]
        _save_global_config(env)
        print(f"✅ 已移除 {key}")
    else:
        print(f"❌ {key} 未配置。")


def _mask_value(key: str, value: str) -> str:
    """Mask sensitive values for display."""
    sensitive = ("KEY", "SECRET", "TOKEN", "PASSWORD")
    if any(s in key.upper() for s in sensitive):
        if len(value) > 8:
            return value[:4] + "****" + value[-4:]
        return "****"
    return value
