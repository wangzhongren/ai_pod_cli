"""Composable decision fragments and a deterministic Canonical Plan reducer."""
from __future__ import annotations

from collections import Counter
from typing import Any


def map_decision_fragments(plan: dict, existing_beans: list[dict]) -> list[dict]:
    """Map loose planner components into a stable, mergeable decision format."""
    known_models = {
        str(bean.get("id")): bean for bean in existing_beans
        if bean.get("category") == "model" and bean.get("id")
    }
    planned_model_ids = {
        str(component.get("name")) for component in plan.get("components", [])
        if component.get("category") == "model" and component.get("name")
    }
    model_ids = set(known_models) | planned_model_ids
    model_alias_candidates: dict[str, set[str]] = {}
    for model_id, bean in known_models.items():
        aliases = {model_id}
        class_path = str(bean.get("class_path", "")).strip()
        if class_path:
            aliases.add(class_path)
            aliases.add(class_path.rsplit(".", 1)[-1])
        for alias in aliases:
            model_alias_candidates.setdefault(alias, set()).add(model_id)
    for model_id in planned_model_ids:
        model_alias_candidates.setdefault(model_id, set()).add(model_id)

    def resolve_model_reference(reference: str) -> str:
        """Resolve a Bean ID, exact class path, or unambiguous class name."""
        candidates = model_alias_candidates.get(reference, set())
        if len(candidates) == 1:
            return next(iter(candidates))
        if "." in reference:
            candidates = model_alias_candidates.get(reference.rsplit(".", 1)[-1], set())
            if len(candidates) == 1:
                return next(iter(candidates))
        return reference

    fragments: list[dict] = []
    for component in plan.get("components", []):
        description = str(component.get("description", ""))
        explicit_models = [
            resolve_model_reference(str(item)) for item in component.get("models", [])
        ]
        raw_dependencies = [str(item) for item in component.get("depends_on", [])]
        resolved_dependencies = [resolve_model_reference(item) for item in raw_dependencies]
        legacy_model_dependencies = [
            item for item in resolved_dependencies if item in model_ids
        ]
        inferred_models = [
            model_id for model_id in model_ids
            if any(
                alias in description
                for alias, candidates in model_alias_candidates.items()
                if candidates == {model_id}
            ) and model_id not in explicit_models
        ]
        fragments.append({
            "id": str(component.get("name", "")),
            "kind": str(component.get("category", "")),
            "decision": description,
            "dependencies": [
                raw for raw, resolved in zip(raw_dependencies, resolved_dependencies)
                if resolved not in model_ids
            ],
            "models": list(dict.fromkeys(
                explicit_models + legacy_model_dependencies + inferred_models
            )),
            "requires": [str(item) for item in component.get("requires", [])],
            "provides": [str(item) for item in component.get("provides", [])],
            "invariants": [str(item) for item in component.get("invariants", [])],
            "status": "proposed",
        })
    return fragments


def _dependency_cycle(fragments: list[dict]) -> list[str]:
    planned = {item["id"] for item in fragments}
    graph = {
        item["id"]: [dep for dep in item["dependencies"] if dep in planned]
        for item in fragments
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, path: list[str]) -> list[str]:
        if node in visiting:
            start = path.index(node)
            return path[start:] + [node]
        if node in visited:
            return []
        visiting.add(node)
        for dependency in graph.get(node, []):
            cycle = visit(dependency, path + [dependency])
            if cycle:
                return cycle
        visiting.remove(node)
        visited.add(node)
        return []

    for node in graph:
        cycle = visit(node, [node])
        if cycle:
            return cycle
    return []


def reduce_decision_fragments(
    plan: dict, existing_beans: list[dict], stage_name: str,
) -> dict[str, Any]:
    """Reduce fragments into one graph or explicit conflicts; never guess a fix."""
    fragments = map_decision_fragments(plan, existing_beans)
    existing = {
        str(bean.get("id")): bean for bean in existing_beans if bean.get("id")
    }
    planned = {item["id"]: item for item in fragments if item["id"]}
    known_ids = set(existing) | set(planned)
    conflicts: list[dict] = []
    warnings: list[dict] = []

    for name, count in Counter(item["id"] for item in fragments).items():
        if not name:
            conflicts.append({"code": "MISSING_DECISION_ID", "message": "A component has no name"})
        elif count > 1:
            conflicts.append({
                "code": "DUPLICATE_DECISION", "component": name,
                "message": f"Component '{name}' is planned {count} times",
            })

    for fragment in fragments:
        component_id = fragment["id"]
        frozen = existing.get(component_id)
        if frozen and frozen.get("category") != fragment["kind"]:
            conflicts.append({
                "code": "FROZEN_CATEGORY_CONFLICT", "component": component_id,
                "message": f"Frozen '{component_id}' is {frozen.get('category')}, not {fragment['kind']}",
            })
        for dependency in fragment["dependencies"]:
            if dependency not in known_ids:
                conflicts.append({
                    "code": "UNKNOWN_DEPENDENCY", "component": component_id,
                    "dependency": dependency,
                    "message": f"'{component_id}' depends on unknown Bean '{dependency}'",
                })
                continue
            dependency_kind = (
                existing[dependency].get("category")
                if dependency in existing else planned[dependency].get("kind")
            )
            if fragment["kind"] == "service" and (
                dependency_kind == "service" or dependency == "PipelineRunner"
            ):
                conflicts.append({
                    "code": "SERVICE_VISIBILITY_VIOLATION",
                    "component": component_id,
                    "dependency": dependency,
                    "message": (
                        f"Service '{component_id}' cannot see Service '{dependency}'; "
                        "compose them in a Pipeline"
                    ),
                })
        for model_id in fragment["models"]:
            if model_id in existing:
                model_kind = existing[model_id].get("category")
            elif model_id in planned:
                model_kind = planned[model_id].get("kind")
            else:
                model_kind = None
            if model_kind != "model":
                conflicts.append({
                    "code": "UNKNOWN_MODEL_REFERENCE", "component": component_id,
                    "model": model_id,
                    "message": f"'{component_id}' references unknown Model '{model_id}'",
                })
        if fragment["kind"] == "service" and not fragment["models"]:
            text = fragment["decision"].lower()
            if any(token in text for token in ("scene", "entities", "collider", "transform", "state")):
                warnings.append({
                    "code": "UNBOUND_COMPLEX_STATE", "component": component_id,
                    "message": "Complex runtime state is described without an explicit frozen Model",
                })

    cycle = _dependency_cycle(fragments)
    if cycle:
        conflicts.append({
            "code": "DEPENDENCY_CYCLE", "path": cycle,
            "message": "Dependency cycle: " + " -> ".join(cycle),
        })

    for fragment in fragments:
        fragment["status"] = "accepted" if not any(
            item.get("component") == fragment["id"] for item in conflicts
        ) else "conflict"

    return {
        "stage": stage_name,
        "status": "accepted" if not conflicts else "conflict",
        "fragments": fragments,
        "conflicts": conflicts,
        "warnings": warnings,
        "graph": {
            item["id"]: {
                "dependencies": item["dependencies"],
                "models": item["models"],
                "requires": item["requires"],
                "provides": item["provides"],
            }
            for item in fragments if item["id"]
        },
    }


def reduce_evidence(violations: list[str]) -> dict:
    """Reduce validation observations into one deterministic build decision."""
    unique = list(dict.fromkeys(str(item) for item in violations))
    return {
        "status": "accepted" if not unique else "repair_current",
        "evidence": unique,
        "repair_scope": "current_candidate" if unique else None,
    }
