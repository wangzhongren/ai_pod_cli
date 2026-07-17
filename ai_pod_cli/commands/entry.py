"""`entry` command — AI generates a project entry point."""

import os

from ai_pod_cli.entry_generator import generate_entry


def _append_deps_to_root_requirements(deps: list[str]):
    """将第三方依赖写入根 requirements.txt，已存在的跳过。"""
    req_path = "requirements.txt"
    existing = set()
    if os.path.exists(req_path):
        with open(req_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    existing.add(line)

    with open(req_path, "a", encoding="utf-8") as f:
        for dep in deps:
            dep = dep.strip()
            if dep and dep not in existing:
                f.write(f"{dep}\n")
                existing.add(dep)


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
            _append_deps_to_root_requirements(extra_deps)
            print(f"📦 额外依赖: {', '.join(extra_deps)}")
            print(f"   安装: pip install {' '.join(extra_deps)}")
        print(f"\n   运行: python {entry_file}")
    else:
        print("\n❌ 入口文件生成失败。")
