"""Resolve imports against the project-wide utility catalog without executing it."""

import ast
from pathlib import Path


def utility_catalog(project_root="."):
    from ai_pod_cli.utilities import list_utilities
    return list_utilities(project_root)


def utility_module(item):
    return Path(item["path"]).with_suffix("").as_posix().replace("/", ".")


def validate_utility_imports(code, project_root="."):
    tree = ast.parse(code)
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "modules.utils" or module.startswith("modules.utils.") or (
                node.level and (module == "utils" or module.startswith("utils."))
            ):
                imports.append((module, [alias.name for alias in node.names], node.level))
        elif isinstance(node, ast.Import):
            imports.extend((alias.name, [], 0) for alias in node.names
                           if alias.name == "modules.utils" or alias.name.startswith("modules.utils."))
    if not imports:
        return []
    try:
        allowed = {utility_module(item): item["symbol"] for item in utility_catalog(project_root)}
    except (OSError, ValueError, TypeError) as error:
        return [f"Utility registry is invalid: {error}"]
    errors = []
    for module, names, relative in imports:
        if relative or module not in allowed:
            errors.append(f"Utility import must use an exact globally registered module: {module}")
        elif any(name != allowed[module] for name in names):
            errors.append(f"Utility {module} exposes only its registered class {allowed[module]}")
    return errors


def utility_summary(project_root="."):
    rows = []
    for item in utility_catalog(project_root):
        rows.append({"id": item["id"], "description": item.get("description", ""),
                     "import": f"from {utility_module(item)} import {item['symbol']}",
                     "methods": item.get("methods", {}), "sha256": item["sha256"]})
    return rows
