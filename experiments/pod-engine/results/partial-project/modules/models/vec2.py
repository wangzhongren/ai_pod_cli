from __future__ import annotations

from math import sqrt
from numbers import Real
from typing import Iterator

from ai_pod_cli import Model


class Vec2(Model):
    """A runtime, non-persistent 2D vector value object."""

    x: float
    y: float

    @staticmethod
    def _validate_number(value: object, field_name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise TypeError(
                f"Vec2.{field_name} must be an int or float, "
                f"got {type(value).__name__}"
            )

    def copy(self) -> Vec2:
        return Vec2(self.x, self.y)

    def __add__(self, other: Vec2) -> Vec2:
        if not isinstance(other, Vec2):
            raise TypeError(
                "Vec2 can only be added to another Vec2, "
                f"not {type(other).__name__}"
            )
        return Vec2(self.x + other.x, self.y + other.y)

    def __sub__(self, other: Vec2) -> Vec2:
        if not isinstance(other, Vec2):
            raise TypeError(
                "Vec2 can only be subtracted from another Vec2, "
                f"not {type(other).__name__}"
            )
        return Vec2(self.x - other.x, self.y - other.y)

    def __mul__(self, scalar: float) -> Vec2:
        self._validate_number(scalar, "scalar")
        return Vec2(self.x * float(scalar), self.y * float(scalar))

    def __rmul__(self, scalar: float) -> Vec2:
        return self.__mul__(scalar)

    def length(self) -> float:
        return sqrt(self.length_squared())

    def length_squared(self) -> float:
        return self.x * self.x + self.y * self.y

    def normalize(self) -> Vec2:
        magnitude = self.length()
        if magnitude == 0:
            return Vec2(0.0, 0.0)
        return Vec2(self.x / magnitude, self.y / magnitude)

    def dot(self, other: Vec2) -> float:
        if not isinstance(other, Vec2):
            raise TypeError(
                "Vec2.dot() expects another Vec2, "
                f"not {type(other).__name__}"
            )
        return self.x * other.x + self.y * other.y

    def __iter__(self) -> Iterator[float]:
        yield self.x
        yield self.y

    def __repr__(self) -> str:
        return f"Vec2(x={self.x!r}, y={self.y!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Vec2):
            return NotImplemented
        return self.x == other.x and self.y == other.y
