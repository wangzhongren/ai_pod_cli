"""Structured computation results for governed AIPod execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Effect:
    """A declared observation of a component's interaction with the outside world."""

    kind: str
    target: str = ""
    operation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "target": self.target,
            "operation": self.operation,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class Success:
    """A successful component result with output and declared effects."""

    output: dict[str, Any] = field(default_factory=dict)
    effects: tuple[Effect, ...] = ()

    @property
    def ok(self) -> bool:
        return True

    def to_dict(self) -> dict:
        return {
            "status": "success",
            "output": dict(self.output),
            "effects": [effect.to_dict() for effect in self.effects],
        }


@dataclass(frozen=True)
class Failure:
    """An expected computation failure that can participate in policies."""

    error: str
    code: str = "component_error"
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)
    effects: tuple[Effect, ...] = ()

    @property
    def ok(self) -> bool:
        return False

    def to_dict(self) -> dict:
        return {
            "status": "failure",
            "error": {
                "code": self.code,
                "message": self.error,
                "retryable": self.retryable,
                "details": dict(self.details),
            },
            "effects": [effect.to_dict() for effect in self.effects],
        }


Result = Success | Failure


def normalize_result(value: Any) -> Result:
    """Adapt legacy component returns to the structured result model."""
    if isinstance(value, (Success, Failure)):
        return value
    if isinstance(value, dict):
        return Success(output=value)
    if value is None:
        return Success()
    return Success(output={"value": value})


def serialize_result(value: Any) -> Any:
    """Return a JSON-friendly representation while preserving legacy dicts."""
    if isinstance(value, (Success, Failure)):
        return value.to_dict()
    return value
