"""CLI entry point — argparse setup and command dispatch."""

import argparse
import sys

from ai_pod_cli.config import init_config_if_not_exists
from ai_pod_cli.commands.add import handle_add
from ai_pod_cli.commands.compose import handle_compose
from ai_pod_cli.commands.create import handle_create
from ai_pod_cli.commands.init import handle_init
from ai_pod_cli.commands.pod import handle_pod


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

    parser = argparse.ArgumentParser(
        description="AIPod — AI-native application framework",
        prog="aipod",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. init
    init_parser = subparsers.add_parser("init", help="Initialize project, AI generates entry point")
    init_parser.add_argument("desc", nargs="?", default="", help="Project description (AI decides tech stack and generates entry file)")
    init_parser.add_argument("--install-deps", action="store_true", help="Auto-install Python dependencies")

    # 2. create
    create_parser = subparsers.add_parser("create", help="AI generates a component")
    create_parser.add_argument("--category", choices=["entry", "entity"], required=True, help="Component category")
    create_parser.add_argument("--name", required=True, help="Component name (must match class name)")
    create_parser.add_argument("--desc", required=True, help="Component description for AI")

    # 3. add
    add_parser = subparsers.add_parser("add", help="Register a hand-written component")
    add_parser.add_argument("--category", choices=["entry", "entity"], default="entity", help="Component category (default: entity)")
    add_parser.add_argument("--name", required=True, help="Component name")
    add_parser.add_argument("--class-path", required=True, help="Full class path, e.g. mypackage.module.MyClass")
    add_parser.add_argument("--desc", required=True, help="Component description")

    # 4. compose
    compose_parser = subparsers.add_parser("compose", help="AI plans and generates a pipeline (no execution)")
    compose_parser.add_argument("cmd", nargs="?", default="", help="Natural language instruction (AI plans the execution chain)")
    compose_parser.add_argument("--name", default="", help="Pipeline file name (auto-generated from instruction if omitted)")
    compose_parser.add_argument("--list", action="store_true", help="List all saved pipelines")

    # 5. pod
    pod_parser = subparsers.add_parser("pod", help="AI decomposes a requirement into a set of components")
    pod_parser.add_argument("desc", help="Feature or system description (AI breaks it into components)")

    args = parser.parse_args()

    if args.command == "init":
        handle_init(args)
    elif args.command == "create":
        handle_create(args)
    elif args.command == "add":
        handle_add(args)
    elif args.command == "compose":
        handle_compose(args)
    elif args.command == "pod":
        handle_pod(args)
