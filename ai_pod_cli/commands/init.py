"""`init` command — initialize project, optionally generate entry point via AI."""

import json
import os
import subprocess
import sys

from ai_pod_cli.config import (
    CONFIG_FILE, CONFIG_TOML, ROUTES_TOML,
    MODULES_DIR, PIPELINES_DIR, init_config_if_not_exists,
)


def handle_init(args):
    """【init 命令】初始化项目：创建目录、配置、可选 AI 生成入口文件"""

    created = []
    skipped = []

    # 1. 创建 modules/ 目录 + 配套文件
    if not os.path.exists(MODULES_DIR):
        os.makedirs(MODULES_DIR)
        created.append(f"📁 目录 {MODULES_DIR}/")
    else:
        skipped.append(f"📁 目录 {MODULES_DIR}/ (已存在)")

    modules_init = os.path.join(MODULES_DIR, "__init__.py")
    if not os.path.exists(modules_init):
        with open(modules_init, "w", encoding="utf-8") as f:
            f.write('"""AI-generated components live here."""\n')
        created.append(f"📄 {modules_init}")
    else:
        skipped.append(f"📄 {modules_init} (已存在)")

    # 2. 创建 pipelines/ 目录
    if not os.path.exists(PIPELINES_DIR):
        os.makedirs(PIPELINES_DIR)
        created.append(f"📁 目录 {PIPELINES_DIR}/")
    else:
        skipped.append(f"📁 目录 {PIPELINES_DIR}/ (已存在)")

    # 3. 创建 config.toml + routes.toml + beans_config.json
    toml_exists = os.path.exists(CONFIG_TOML)
    routes_exists = os.path.exists(ROUTES_TOML)
    beans_exists = os.path.exists(CONFIG_FILE)

    if not toml_exists or not routes_exists or not beans_exists:
        init_config_if_not_exists()

    if not toml_exists:
        created.append(f"📄 {CONFIG_TOML} (项目配置，组件通过 ConfigStore 读取)")
    else:
        skipped.append(f"📄 {CONFIG_TOML} (已存在)")

    if not routes_exists:
        created.append(f"📄 {ROUTES_TOML} (路由配置，compose 自动注册 pipeline)")
    else:
        skipped.append(f"📄 {ROUTES_TOML} (已存在)")

    if not beans_exists:
        created.append(f"📄 {CONFIG_FILE} (含 ConfigStore, DbClient, SmsSender)")
    else:
        skipped.append(f"📄 {CONFIG_FILE} (已存在)")

    # 4. .gitignore
    gitignore = ".gitignore"
    if not os.path.exists(gitignore):
        with open(gitignore, "w", encoding="utf-8") as f:
            f.write("# OS\n.DS_Store\nThumbs.db\n\n# IDE\n.vscode/\n.idea/\n\n# Python\n__pycache__/\n*.pyc\n")
        created.append(f"📄 {gitignore}")
    else:
        skipped.append(f"📄 {gitignore} (已存在)")

    # 6. 安装依赖
    if args.install_deps:
        print("\n📦 [初始化] 正在安装 Python 依赖 (openai, injector, python-dotenv, tomlkit)...")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install",
                 "openai", "injector", "python-dotenv", "tomlkit", "-q"],
            )
            created.append("📦 依赖 openai, injector, python-dotenv, tomlkit")
        except subprocess.CalledProcessError as e:
            print(f"   ⚠️  依赖安装失败: {e}")
    else:
        skipped.append("📦 依赖安装 (跳过，使用 --install-deps 启用)")

    # 输出汇总
    print("\n🎉 [初始化完成] 项目就绪！\n")

    if created:
        print("   ✅ 已创建:")
        for item in created:
            print(f"      {item}")

    if skipped:
        print("\n   ⏭️  已跳过:")
        for item in skipped:
            print(f"      {item}")

    print(f"\n   下一步:")
    print(f"   aipod config set OPENAI_API_KEY sk-your-key   (首次使用)")
    print(f"   aipod pod \"需求描述\"                          (一步生成组件+Pipeline+入口)")
    print(f"   aipod create --category entity --name X --desc \"...\"  (逐步构建)")

