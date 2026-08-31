"""Human-readable snapshots of frozen Pod stage plans."""


def save_pod_plan(
    pod_name: str, desc: str, components: list, pipelines: list,
    config_additions: dict, interfaces: list | None = None,
):
    """将拆解方案保存为 Markdown 文件，方便人阅读和后续 AI 参考。"""
    from datetime import datetime

    lines = [
        f"# Pod Plan: {pod_name}",
        "",
        f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 需求描述",
        "",
        desc,
        "",
        "## 组件拆解",
        "",
        f"共 {len(components)} 个组件：",
        "",
    ]

    for i, comp in enumerate(components, 1):
        deps = comp.get("depends_on", [])
        dep_str = f" ← depends: {', '.join(deps)}" if deps else ""
        lines.append(f"### {i}. {comp['name']} ({comp['category']}){dep_str}")
        lines.append("")
        lines.append(comp.get("description", ""))
        lines.append("")

    if pipelines:
        lines.append("## Pipeline 规划")
        lines.append("")
        for i, pipe in enumerate(pipelines, 1):
            lines.append(f"### {i}. {pipe.get('name', '')}")
            lines.append(f"> {pipe.get('instruction', '')}")
            lines.append("")

    if interfaces:
        lines.append("## Interface 规划")
        lines.append("")
        for i, interface in enumerate(interfaces, 1):
            lines.append(f"### {i}. {interface.get('name', '')} ({interface.get('kind', '')})")
            lines.append(f"> {interface.get('instruction', '')}")
            lines.append("")

    if config_additions:
        lines.append("## 建议新增配置")
        lines.append("")
        lines.append("```toml")
        for section, keys in config_additions.items():
            lines.append(f"[{section}]")
            for key, raw_value in keys.items():
                if isinstance(raw_value, dict):
                    val = raw_value.get("value", "")
                    comment = raw_value.get("comment", "")
                    lines.append(f"{key} = {val}  # {comment}")
                else:
                    lines.append(f"{key} = {raw_value}")
        lines.append("```")
        lines.append("")

    filename = f"{pod_name}_plan.md"
    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"📋 [方案已保存] {filename}\n")
