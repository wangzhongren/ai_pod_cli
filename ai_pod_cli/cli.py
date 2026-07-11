"""CLI entry point — argparse setup and command dispatch."""

import argparse
import sys

from ai_pod_cli.config import init_config_if_not_exists
from ai_pod_cli.commands.add import handle_add
from ai_pod_cli.commands.build import handle_build
from ai_pod_cli.commands.create import handle_create
from ai_pod_cli.commands.init import handle_init


def main():
    """Main CLI entry point for the AI Pod engine."""
    # 从 .env 文件加载环境变量（优先于系统环境变量）
    from dotenv import load_dotenv
    load_dotenv()

    # Windows 终端 GBK 兼容：强制 UTF-8 输出
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
        sys.stderr.reconfigure(encoding="utf-8")

    # init 命令自行管理初始化，其余命令走自动初始化
    if len(sys.argv) > 1 and sys.argv[1] != "init":
        init_config_if_not_exists()

    parser = argparse.ArgumentParser(description="AI 原生 IoC 容器低代码引擎 CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. init 命令定义
    init_parser = subparsers.add_parser("init", help="初始化项目：创建目录、配置，可选 AI 生成入口文件")
    init_parser.add_argument("desc", nargs="?", default="", help="项目描述（AI 据此判断技术栈并生成入口文件）")
    init_parser.add_argument("--install-deps", action="store_true", help="自动安装 Python 依赖")

    # 2. create 命令定义
    create_parser = subparsers.add_parser("create", help="让 AI 创造一个组件并记录配置")
    create_parser.add_argument("--category", choices=["entry", "entity"], required=True, help="组件分类")
    create_parser.add_argument("--name", required=True, help="组件名称")
    create_parser.add_argument("--desc", required=True, help="给AI传入的需求和描述")

    # 3. add 命令定义
    add_parser = subparsers.add_parser("add", help="注册一个人工编写的实体组件到配置中心")
    add_parser.add_argument("--category", choices=["entry", "entity"], default="entity", help="组件分类（默认 entity）")
    add_parser.add_argument("--name", required=True, help="组件名称（必须与类名一致）")
    add_parser.add_argument("--class-path", required=True, help="完整的类路径，如 mypackage.mymodule.MyClass")
    add_parser.add_argument("--desc", required=True, help="组件功能描述")

    # 4. build 命令定义
    build_parser = subparsers.add_parser("build", help="AI 生成 pipeline 文件并注册到 routes.toml（只生成不执行）")
    build_parser.add_argument("cmd", nargs="?", default="", help="自然语言业务指令（AI 据此规划执行链）")
    build_parser.add_argument("--name", default="", help="保存 pipeline 的文件名（默认从指令自动生成）")
    build_parser.add_argument("--list", action="store_true", help="列出所有已保存的 pipeline")

    args = parser.parse_args()

    if args.command == "init":
        handle_init(args)
    elif args.command == "create":
        handle_create(args)
    elif args.command == "add":
        handle_add(args)
    elif args.command == "build":
        handle_build(args)
