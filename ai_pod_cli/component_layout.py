"""Static source resolution for contracts/impl/public component directories."""

from __future__ import annotations

import ast
from pathlib import Path

from ai_pod_cli.validation import validate_component_contract, validate_service_helpers

AREAS = ("contracts", "impl", "public")


def area(path: str) -> tuple[str, str]:
    parts = Path(path).parts
    if len(parts) > 2 and parts[0] == "modules" and parts[1] in {"providers", "services"}:
        return parts[1], parts[2] if parts[2] in AREAS else "legacy"
    return "", ""


class SourceGraph:
    """Follow ordinary imports without importing or executing project code."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.cache = {}
        self.edges = {}

    def source(self, path):
        if path not in self.cache:
            target = self.root / path
            if target.resolve() != target or not target.is_relative_to(self.root):
                raise ValueError(f"Source must stay inside the project without symlinks: {path}")
            code = target.read_text(encoding="utf-8")
            self.cache[path] = (code, ast.parse(code, filename=path))
        return self.cache[path]

    def module_file(self, module):
        if not module or not all(part.isidentifier() for part in module.split(".")):
            return None
        for path in (module.replace(".", "/") + ".py", module.replace(".", "/") + "/__init__.py"):
            if (self.root / path).is_file():
                self.source(path)
                return path
        return None

    def import_module(self, path, node):
        if not node.level:
            return node.module or ""
        package = Path(path).parent.parts
        if node.level > len(package):
            raise ValueError(f"Relative import escapes project: {path}")
        parent = package[:len(package) - node.level + 1]
        suffix = tuple(node.module.split(".")) if node.module else ()
        return ".".join((*parent, *suffix))

    def imports(self, path):
        if path in self.edges:
            return self.edges[path]
        result = []
        for node in ast.walk(self.source(path)[1]):
            if isinstance(node, ast.ImportFrom):
                module = self.import_module(path, node)
                for alias in node.names:
                    child = self.module_file(f"{module}.{alias.name}")
                    target = child or self.module_file(module)
                    if target:
                        result.append((target, None if child else alias.name))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    target = self.module_file(alias.name)
                    if target:
                        result.append((target, None))
            elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                if isinstance(node.func, ast.Name) and node.func.id == "__import__" or isinstance(node.func, ast.Attribute) and node.func.attr == "import_module":
                    target = self.module_file(node.args[0].value)
                    if target:
                        result.append((target, None))
        self.edges[path] = result
        return result

    def exported_class(self, path, name, seen=None):
        seen = set() if seen is None else set(seen)
        if (path, name) in seen:
            raise ValueError(f"Cyclic public export: {path}:{name}")
        seen.add((path, name))
        tree = self.source(path)[1]
        if area(path)[1] == "public":
            for node in tree.body:
                doc = isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
                exports = isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
                if exports:
                    value = ast.literal_eval(node.value)
                    exports = isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value)
                if not (doc or exports or isinstance(node, (ast.ImportFrom, ast.Import))):
                    raise ValueError(f"public contains only imports, docstrings and literal __all__; put implementation in impl: {path}")
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == name:
                return path, name
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if (alias.asname or alias.name) == name:
                        target = self.module_file(self.import_module(path, node))
                        if target:
                            if Path(target).parts[:2] != Path(path).parts[:2]:
                                raise ValueError("Public exports must resolve to the same layer")
                            return self.exported_class(target, alias.name, seen)
        raise ValueError(f"Cannot resolve class {name} from {path}; use explicit named imports for public exports")

    def has_export(self, path, name):
        for node in self.source(path)[1].body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return True
            if isinstance(node, (ast.Import, ast.ImportFrom)) and any((alias.asname or alias.name) in {name, "*"} for alias in node.names):
                return True
        return False

    def closure(self, path):
        found, pending = set(), [path]
        while pending:
            current = pending.pop()
            if current in found:
                continue
            found.add(current)
            pending.extend(target for target, _ in self.imports(current)
                           if Path(target).parts[:2] == Path(path).parts[:2])
        return found

    def component(self, bean):
        module, name = bean["class_path"].rsplit(".", 1)
        entry = self.module_file(module)
        if not entry:
            raise ValueError(f"Missing component module: {module}")
        if area(entry)[1] in {"contracts", "impl"}:
            raise ValueError("Register components through public/, not contracts/ or impl/")
        implementation, symbol = self.exported_class(entry, name)
        if area(entry)[1] == "public" and area(implementation)[1] != "impl":
            raise ValueError("Public component exports must resolve to a class in impl/")
        return entry, implementation, symbol


def validate_layout(root, beans, *, stage=None):
    """Validate owned sources and registrations, including unregistered helpers."""
    graph, errors = SourceGraph(root), []
    components = {}
    for bean in beans:
        if not bean.get("class_path", "").startswith("modules."):
            continue
        if bean.get("category") not in {"provider", "service"}:
            continue
        try:
            components[bean["id"]] = graph.component(bean)
        except (ValueError, OSError, SyntaxError) as error:
            if stage is None or bean.get("category") + "s" == stage:
                errors.append(f"{bean['id']}: {error}")
    service_files = {components[bean["id"]][1] for bean in beans
                     if bean.get("category") == "service" and bean["id"] in components}
    for bean in beans:
        if bean["id"] not in components or stage and bean["category"] + "s" != stage:
            continue
        entry, implementation, symbol = components[bean["id"]]
        try:
            code = graph.source(implementation)[0]
            errors.extend(f"{implementation}: {error}" for error in validate_component_contract(
                code, symbol, bean["category"], bean.get("inputs", {}), bean.get("outputs", {}),
                bean.get("methods", {}), allow_internal_imports=True))
            if bean["category"] == "service":
                for path in graph.closure(implementation):
                    errors.extend(f"{path}: {error}" for error in validate_service_helpers(graph.source(path)[0]))
                    if path in service_files and path != implementation:
                        errors.append(f"{implementation}: Service cannot import another Service ({path}); compose in a Pipeline")
        except (ValueError, OSError, SyntaxError) as error:
            errors.append(f"{entry}: {error}")
    roots = [f"modules/{stage}"] if stage in {"models", "providers", "services"} else [stage] if stage else ["modules", "pipelines", "interfaces"]
    for base in roots:
        for file in sorted((graph.root / base).rglob("*.py")):
            path = file.relative_to(graph.root).as_posix()
            try:
                owner, section = area(path)
                for target, _ in graph.imports(path):
                    target_owner, target_section = area(target)
                    if target_section == "impl" and owner != target_owner:
                        errors.append(f"{path}: import {target_owner} through public/ or contracts/, not {target}")
                    if section == "contracts" and target_section in {"impl", "public"}:
                        errors.append(f"{path}: contracts cannot depend on implementation/public ({target})")
            except (ValueError, OSError, SyntaxError) as error:
                errors.append(f"{path}: {error}")
    return list(dict.fromkeys(errors))
