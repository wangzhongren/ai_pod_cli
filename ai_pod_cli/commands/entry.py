"""`entry` command — AI generates a project entry point."""

import os

from ai_pod_cli.entry_generator import generate_entry


def handle_entry(args):
    """【entry 命令】AI 根据描述生成项目入口文件"""
    desc = args.desc
    if not desc:
        print("❌ 请提供项目描述。")
        print("   用法: aipod entry \"一个 Flask REST API\"")
        return

    entry_info = generate_entry(desc)
    if entry_info:
        entry_file, extra_deps = entry_info
        print(f"\n🎉 入口文件已生成: {entry_file}")
        if extra_deps:
            modules_req = os.path.join("modules", "requirements.txt")
            if os.path.exists(modules_req):
                with open(modules_req, "a", encoding="utf-8") as f:
                    for dep in extra_deps:
                        f.write(f"{dep}\n")
            print(f"📦 额外依赖: {', '.join(extra_deps)}")
            print(f"   安装: pip install {' '.join(extra_deps)}")
        print(f"\n   运行: python {entry_file}")
    else:
        print("\n❌ 入口文件生成失败。")
