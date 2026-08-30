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


def apply_file_patches(code: str, patches) -> str:
    """Apply bounded exact patches to a Pipeline or Interface module."""
    if not isinstance(patches, list) or not patches:
        raise ValueError("AI 未返回 patches")
    original_tree = ast.parse(code)
    required_symbols = {
        (type(node).__name__, node.name)
        for node in original_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
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
        total_old += len(old)
        replaces_whole_candidate = replaces_whole_candidate or old.strip() == code.strip()
        updated = updated.replace(old, new, 1)
    if replaces_whole_candidate or total_old > max(400, int(len(code) * 0.4)):
        raise ValueError("补丁范围过大，拒绝近似重写整个文件")
    updated_tree = ast.parse(updated)
    updated_symbols = {
        (type(node).__name__, node.name)
        for node in updated_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing = sorted(required_symbols - updated_symbols)
    if missing:
        raise ValueError(f"补丁不得删除或重命名现有符号: {missing}")
    return updated


def file_patch_prompt(code: str, evidence: list[str], relative_path: str) -> str:
    """Build a constrained repair request for one evidence-selected file."""
    details = "\n".join(f"- {item}" for item in evidence)
    return f"""
应用验证已经失败。只允许修复 traceback 指向的当前文件：{relative_path}
此前通过的 Model、Provider、Service、Pipeline 和 Interface 均保持冻结，除非它们就是该文件。

验证证据：
{details}

当前文件：
<candidate path="{relative_path}">
{code}
</candidate>

只返回 JSON：
{{"patches":[{{"old":"文件中精确存在的最小旧文本","new":"替换后的文本"}}]}}

规则：
- 只修复证据直接证明的问题，每个 old 必须逐字且只出现一次。
- 禁止返回完整 code，禁止近似重写文件，禁止顺手重构。
- 禁止修改凭据、配置、Contract、Bean ID、类名、路由名或其他文件。
- 必须保持当前公开入口、函数和类存在。
"""
