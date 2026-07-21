"""`add` command — register a hand-written component into the bean config."""

from ai_pod_cli.config import load_config, save_config


def handle_add(args):
    """【add 命令】将人工编写的组件注册到配置中心"""
    print(f"📦 [CLI] 正在注册人工组件: '{args.name}'...")

    config = load_config()

    # 检查是否已存在同名组件，存在则覆盖
    config["beans"] = [b for b in config["beans"] if b["id"] != args.name]

    new_bean = {
        "id": args.name,
        "category": args.category,
        "type": "human_added",
        "class_path": args.class_path,
        "description": args.desc,
    }

    config["beans"].append(new_bean)
    save_config(config)

    print(f"💾 [注册成功] 组件 '{args.name}' 已写入配置中心 (class_path: {args.class_path})")
    print(f"   分类: {args.category}")
    print(f"   描述: {args.desc}")
