"""Typed component contracts and static pipeline composition analysis."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_TYPE_ALIASES = {
    "integer": "int", "number": "float", "boolean": "bool",
    "string": "str", "object": "dict", "array": "list",
    "mapping": "dict", "none": "null", "any": "any",
}


def normalize_type(spec: Any) -> str:
    """Extract a stable type token from legacy or structured field metadata."""
    if isinstance(spec, dict):
        spec = spec.get("type", "any")
    if not isinstance(spec, str) or not spec.strip():
        return "any"
    token = re.split(r"\s*(?:—|–|-|:)\s*", spec.strip(), maxsplit=1)[0]
    token = re.sub(r"\s+", "", token).lower()
    return _TYPE_ALIASES.get(token, token or "any")


def types_compatible(produced: str, required: str) -> bool:
    """Return whether a produced value may satisfy a required field type."""
    produced, required = normalize_type(produced), normalize_type(required)
    if "any" in (produced, required) or produced == required:
        return True
    # An int is valid wherever a general numeric float is accepted.
    return produced == "int" and required == "float"


@dataclass(frozen=True)
class ContractField:
    name: str
    type: str = "any"
    required: bool = True
    description: str = ""

    @classmethod
    def from_spec(cls, name: str, spec: Any) -> "ContractField":
        if isinstance(spec, dict):
            return cls(
                name=name,
                type=normalize_type(spec),
                required=bool(spec.get("required", "default" not in spec)),
                description=str(spec.get("description", "")),
            )
        text = str(spec or "")
        parts = re.split(r"\s*(?:—|–)\s*", text, maxsplit=1)
        return cls(name=name, type=normalize_type(text), description=parts[1] if len(parts) > 1 else "")

    def as_dict(self) -> dict:
        return {
            "name": self.name, "type": self.type,
            "required": self.required, "description": self.description,
        }


def fields_from_metadata(metadata: Any) -> dict[str, ContractField]:
    if not isinstance(metadata, dict):
        return {}
    return {str(name): ContractField.from_spec(str(name), spec) for name, spec in metadata.items()}


def analyze_pipeline_contracts(service_ids: list[str], components: list[dict]) -> dict:
    """Infer a pipeline contract and validate the types flowing through its context.

    Inputs not produced by an earlier component become external pipeline inputs.
    A same-named value with an incompatible type is a composition error.
    """
    by_id = {item.get("id"): item for item in components}
    available: dict[str, ContractField] = {}
    external: dict[str, ContractField] = {}
    links: list[dict] = []
    issues: list[dict] = []

    for index, service_id in enumerate(service_ids):
        component = by_id.get(service_id)
        if component is None:
            continue
        inputs = fields_from_metadata(component.get("inputs", {}))
        matched, missing, mismatches = [], [], []
        for name, required in inputs.items():
            produced = available.get(name)
            if produced is None:
                if required.required:
                    external.setdefault(name, required)
                    missing.append(name)
                continue
            if types_compatible(produced.type, required.type):
                matched.append(name)
            else:
                mismatch = {"field": name, "produced": produced.type, "required": required.type}
                mismatches.append(mismatch)
                issues.append({
                    "code": "contract_type_mismatch", "component": service_id,
                    "field": name, "produced": produced.type, "required": required.type,
                })
        if index:
            links.append({
                "from": service_ids[index - 1], "to": service_id,
                "compatible": not mismatches, "matched": matched,
                "external_inputs": missing, "mismatches": mismatches,
            })
        available.update(fields_from_metadata(component.get("outputs", {})))

    return {
        "inputs": {name: field.as_dict() for name, field in external.items()},
        "outputs": {name: field.as_dict() for name, field in available.items()},
        "links": links,
        "valid": not issues,
        "issues": issues,
    }
