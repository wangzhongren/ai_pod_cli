"""The single AIPod data contract, backed by Pydantic and SQLAlchemy."""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
from enum import Enum
from types import UnionType
from typing import Any, Union, get_args, get_origin
from uuid import UUID

from pydantic import ValidationError
from sqlmodel import SQLModel


def _sample_type(annotation: Any) -> Any:
    origin, args = get_origin(annotation), get_args(annotation)
    if annotation is Any:
        return None
    if origin is list:
        return [_sample_type(args[0] if args else Any)]
    if origin is set:
        return {_sample_type(args[0] if args else Any)}
    if origin is tuple:
        return tuple(_sample_type(item) for item in args)
    if origin is dict:
        return {}
    if origin in (Union, UnionType):
        if type(None) in args:
            return None
        option = next(iter(args), type(None))
        return None if option is type(None) else _sample_type(option)
    if isinstance(annotation, type) and issubclass(annotation, Model):
        return annotation.sample_instance()
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return next(iter(annotation))
    return {str: "test", bool: False, int: 1, float: 1.0, bytes: b"test",
            date: date(2024, 1, 1), time: time(12, 0),
            datetime: datetime(2024, 1, 1, tzinfo=timezone.utc),
            Decimal: Decimal("1.0"), UUID: UUID("00000000-0000-0000-0000-000000000001")}.get(annotation)


class Model(SQLModel):
    """Base for DTOs and persistent ``table=True`` models."""

    @classmethod
    def validate(cls, value: Any, path: str = "$") -> list[str]:
        try:
            cls.model_validate(value)
            return []
        except ValidationError as error:
            return [
                f"{path}" + "".join(
                    f"[{part}]" if isinstance(part, int) else f".{part}"
                    for part in item.get("loc", ())
                ) + f": {'required field is missing' if item.get('type') == 'missing' else item.get('msg', 'invalid value')}"
                for item in error.errors()
            ]

    def to_dict(self) -> dict:
        return self.model_dump()

    @classmethod
    def sample(cls) -> dict:
        return {
            name: _sample_type(field.annotation)
            for name, field in cls.model_fields.items()
            if field.is_required()
        }

    @classmethod
    def sample_instance(cls) -> "Model":
        return cls.model_validate(cls.sample())
