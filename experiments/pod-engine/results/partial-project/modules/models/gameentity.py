from dataclasses import dataclass

from ai_pod_cli import Model
from modules.models.vec2 import Vec2


@dataclass
class GameEntity(Model):
    """Runtime value model for a simulated game entity.

    This is intentionally not a persistent model and must not be registered
    as a SQLModel table or stored through ModelRepository.
    """

    id: str
    kind: str
    position: Vec2
    velocity: Vec2
    size: Vec2
    shape: str = "rect"
    enabled: bool = True
    static: bool = False

    @property
    def center(self) -> Vec2:
        """Alias for the entity center point."""
        return self.position

    @property
    def radius(self) -> float:
        """Radius of a circle-shaped entity."""
        if self.shape != "circle":
            raise AttributeError("radius is only available when shape == 'circle'")
        return self.size.x / 2.0

    def copy(self) -> "GameEntity":
        """Return an independent copy of this entity."""
        return GameEntity(
            id=self.id,
            kind=self.kind,
            position=self.position.copy(),
            velocity=self.velocity.copy(),
            size=self.size.copy(),
            shape=self.shape,
            enabled=self.enabled,
            static=self.static,
        )
