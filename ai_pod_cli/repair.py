"""Constrained candidate repair for AI-generated components."""

from __future__ import annotations

import ast


def classify_failures(violations: list[str]) -> str:
    text = "\n".join(violations).lower()
    if any(token in text for token in ("modulenotfounderror", "importerror", "cannot import")):
        return "import"
    if any(token in text for token in ("依赖 id", "dependency", "class_path")):
        return "dependency"
    if any(token in text for token in ("inputs.", "outputs.", "schema", "contract", "未在 inputs", "未在 outputs")):
        return "contract"
    if any(token in text for token in ("traceback", "沙箱", "runtime", "attributeerror", "typeerror")):
        return "runtime"
    return "static"


def can_patch_code(failure_kind: str) -> bool:
    return failure_kind in {"import", "runtime", "static"}


def patch_prompt(code: str, violations: list[str], failure_kind: str) -> str:
    details = "\n".join(f"- {item}" for item in violations)
    return f"""
当前候选组件已经生成，大部分代码是稳定的。禁止重新生成完整组件。
错误分类: {failure_kind}
错误:
{details}

当前候选代码:
<candidate>
{code}
</candidate>

只返回 JSON：
{{"patches":[{{"old":"候选中精确存在的最小旧文本","new":"替换后的文本"}}]}}

规则：
- 每个 old 必须逐字存在，只修改解决错误所需的最小片段。
- 禁止返回 code、dependencies、inputs、outputs 或完整文件。
- 禁止修改类名、删除主体、顺手重构或改变无关方法。
- import 错误只修改 import 行或引用该 import 的最小表达式。
"""


def apply_code_patches(code: str, patches, class_name: str, failure_kind: str) -> str:
    """Apply small exact replacements while preserving candidate invariants."""
    if not isinstance(patches, list) or not patches:
        raise ValueError("AI 未返回 patches")
    updated = code
    total_old = 0
    replaces_whole_candidate = False
    for index, patch in enumerate(patches, 1):
        if not isinstance(patch, dict):
            raise ValueError(f"patch {index} 不是对象")
        old, new = patch.get("old"), patch.get("new")
        if not isinstance(old, str) or not old:
            raise ValueError(f"patch {index}.old 不能为空")
        if not isinstance(new, str):
            raise ValueError(f"patch {index}.new 必须是字符串")
        if updated.count(old) != 1:
            raise ValueError(f"patch {index}.old 必须在候选中精确出现一次")
        if (
            failure_kind == "import" and "import" not in old and "import" not in new
            and "." not in old and "." not in new
        ):
            raise ValueError("import 修复只能修改 import 或其引用")
        total_old += len(old)
        replaces_whole_candidate = replaces_whole_candidate or old.strip() == code.strip()
        updated = updated.replace(old, new, 1)
    if replaces_whole_candidate or total_old > max(400, int(len(code) * 0.4)):
        raise ValueError("补丁范围过大，拒绝近似重写整个组件")
    tree = ast.parse(updated)
    if not any(isinstance(node, ast.ClassDef) and node.name == class_name for node in tree.body):
        raise ValueError(f"补丁不得删除或重命名类 {class_name}")
    return updated
