"""PngSnapshotService renders the current Breakout world to a PNG file.

The renderer is headless: it initialises only pygame's display subsystem with the
dummy video driver and never opens a visible window.
"""

import os
from typing import Tuple

from injector import inject

from ai_pod_cli.config_store import ConfigStore
from ai_pod_cli.context import PipelineContext

_BACKGROUND_COLOR = (15, 17, 32)
_BALL_COLOR = (250, 250, 250)
_PADDLE_COLOR = (80, 160, 255)
_BRICK_COLOR = (220, 70, 90)


class PngSnapshotService:
    """Service that saves the currently simulated Breakout frame as a PNG."""

    @inject
    def __init__(self, config_store: ConfigStore) -> None:
        self._config_store = config_store

    def execute(self, ctx: PipelineContext) -> dict:
        """Read the frame from the context, draw it, and persist the PNG.

        The returned dict and the value written via ``ctx.set`` expose the
        actual screenshot path written to disk.
        """
        screenshot_path = self._resolve_screenshot_path(ctx)
        entities = ctx.get("entities")
        if entities is None:
            raise RuntimeError(
                "PngSnapshotService.execute requires ctx.entities to contain "
                "the current scene entities."
            )

        width, height = self._screen_size()
        pygame = self._init_pygame()

        surface = pygame.Surface((width, height))
        surface.fill(_BACKGROUND_COLOR)

        for entity in entities:
            if entity is None or not entity.enabled:
                continue

            pos_x = entity.position.x
            pos_y = entity.position.y
            size_x = entity.size.x
            size_y = entity.size.y

            if entity.kind == "ball":
                radius = max(1, int(round(size_x / 2.0)))
                pygame.draw.circle(
                    surface,
                    _BALL_COLOR,
                    (int(round(pos_x)), int(round(pos_y))),
                    radius,
                )
            elif entity.kind in ("paddle", "brick"):
                color = _PADDLE_COLOR if entity.kind == "paddle" else _BRICK_COLOR
                rect = pygame.Rect(
                    int(round(pos_x - size_x / 2.0)),
                    int(round(pos_y - size_y / 2.0)),
                    max(1, int(round(size_x))),
                    max(1, int(round(size_y))),
                )
                pygame.draw.rect(surface, color, rect)

        output_dir = os.path.dirname(os.path.abspath(screenshot_path))
        os.makedirs(output_dir, exist_ok=True)

        pygame.image.save(surface, screenshot_path)
        ctx.set("screenshot_path", screenshot_path)

        return {"screenshot_path": screenshot_path}

    def _resolve_screenshot_path(self, ctx: PipelineContext) -> str:
        """Return a screenshot destination from the context or config store."""
        path = ctx.get("screenshot_path")
        if path:
            return str(path)

        configured_path = self._config_store.get("run.screenshot_path")
        if configured_path:
            return str(configured_path)

        raise RuntimeError(
            "PngSnapshotService could not resolve a screenshot path: neither "
            "ctx.screenshot_path nor config run.screenshot_path is set."
        )

    def _screen_size(self) -> Tuple[int, int]:
        """Read and validate the configured render surface dimensions."""
        width = self._config_store.get("game.screen_width")
        height = self._config_store.get("game.screen_height")

        if width is None or height is None:
            raise RuntimeError(
                "PngSnapshotService requires config game.screen_width and "
                "game.screen_height."
            )

        try:
            width = int(width)
            height = int(height)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "game.screen_width and game.screen_height must be numeric."
            ) from exc

        if width <= 0 or height <= 0:
            raise RuntimeError(
                "game.screen_width and game.screen_height must be positive."
            )

        return width, height

    def _init_pygame(self):
        """Initialise pygame headlessly using the dummy video driver."""
        os.environ["SDL_VIDEODRIVER"] = "dummy"
        import pygame

        if not pygame.display.get_init():
            pygame.display.init()

        return pygame
