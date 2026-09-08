from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from injector import inject

from ai_pod_cli.config_store import ConfigStore
from ai_pod_cli.context import PipelineContext

from modules.models.gameentity import GameEntity
from modules.models.vec2 import Vec2


class BreakoutSimulationService:
    """Deterministic fixed-timestep Breakout simulation service.

    The service does not render or read hardware input.  It consumes a
    PipelineContext carrying optional command booleans and optionally an
    existing game snapshot, then writes the next simulation snapshot back
    through the same context.
    """

    _COMMAND_KEYS = ("left", "right", "launch", "pause", "reset")
    _VALID_STATES = ("READY", "PLAYING", "PAUSED", "WON", "LOST")

    @inject
    def __init__(self, config_store: ConfigStore) -> None:
        self._cfg = config_store

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def execute(self, ctx: PipelineContext) -> dict:
        commands = self._normalize_commands(self._safe_get(ctx, "commands"))
        previous = self._normalize_commands(
            self._safe_get(ctx, "previous_commands")
        )

        reset_edge = commands["reset"] and not previous["reset"]
        pause_edge = commands["pause"] and not previous["pause"]
        launch_edge = commands["launch"] and not previous["launch"]

        frame_count = self._to_int(self._safe_get(ctx, "frame_count"), 0) + 1

        # Reset always rebuilds the scene and consumes the reset edge.
        if reset_edge:
            world = self._create_world()
            return self._commit(
                ctx,
                entities=world["entities"],
                game_state=world["game_state"],
                score=world["score"],
                lives=world["lives"],
                ball_attached=world["ball_attached"],
                simulation_time=world["simulation_time"],
                frame_count=frame_count,
                events=["reset"],
            )

        raw_entities = self._safe_get(ctx, "entities")
        if not raw_entities:
            world = self._create_world()
            entities = self._coerce_entities(world["entities"])
            game_state = world["game_state"]
            score = world["score"]
            lives = world["lives"]
            ball_attached = world["ball_attached"]
            simulation_time = world["simulation_time"]
        else:
            entities = self._coerce_entities(raw_entities)
            game_state = self._normalize_state(
                self._safe_get(ctx, "game_state")
            )
            score = self._to_int(self._safe_get(ctx, "score"), 0)
            lives = self._to_int(self._safe_get(ctx, "lives"), self._base_lives())
            raw_ball_attached = self._safe_get(ctx, "ball_attached")
            ball_attached = self._to_bool(
                raw_ball_attached, game_state == "READY"
            )
            simulation_time = self._to_float(
                self._safe_get(ctx, "simulation_time"), 0.0
            )

        # Pause edge handling.  Entering PAUSED cannot change physics in the
        # same execute call.
        if pause_edge:
            if game_state == "PAUSED":
                # The ball_attached flag tells us whether we paused from READY
                # or PLAYING.
                game_state = "READY" if ball_attached else "PLAYING"
            elif game_state in ("READY", "PLAYING"):
                game_state = "PAUSED"
                return self._commit(
                    ctx,
                    entities=entities,
                    game_state=game_state,
                    score=score,
                    lives=lives,
                    ball_attached=ball_attached,
                    simulation_time=simulation_time,
                    frame_count=frame_count,
                    events=[],
                )

        if game_state in ("PAUSED", "WON", "LOST"):
            return self._commit(
                ctx,
                entities=entities,
                game_state=game_state,
                score=score,
                lives=lives,
                ball_attached=ball_attached,
                simulation_time=simulation_time,
                frame_count=frame_count,
                events=[],
            )

        dt = self._resolve_dt(ctx)
        events: List[str] = []
        paddle = self._find_entity(entities, "paddle")
        ball = self._find_entity(entities, "ball")

        if game_state == "READY":
            self._move_paddle(paddle, commands["left"], commands["right"], dt)
            if ball_attached and paddle is not None and ball is not None:
                self._attach_ball(ball, paddle)

            if launch_edge and ball is not None:
                self._apply_launch_velocity(ball)
                ball_attached = False
                game_state = "PLAYING"
            else:
                return self._commit(
                    ctx,
                    entities=entities,
                    game_state=game_state,
                    score=score,
                    lives=lives,
                    ball_attached=ball_attached,
                    simulation_time=simulation_time,
                    frame_count=frame_count,
                    events=events,
                )

        # From here on the simulation only runs in PLAYING state.
        if game_state != "PLAYING":
            return self._commit(
                ctx,
                entities=entities,
                game_state=game_state,
                score=score,
                lives=lives,
                ball_attached=ball_attached,
                simulation_time=simulation_time,
                frame_count=frame_count,
                events=events,
            )

        screen_width = float(self._cfg.get("game.screen_width", 800))
        screen_height = float(self._cfg.get("game.screen_height", 600))

        if paddle is not None:
            self._move_paddle(
                paddle, commands["left"], commands["right"], dt
            )

        if ball is None:
            return self._commit(
                ctx,
                entities=entities,
                game_state=game_state,
                score=score,
                lives=lives,
                ball_attached=ball_attached,
                simulation_time=simulation_time,
                frame_count=frame_count,
                events=events,
            )

        # Physics is active only when a ball is being simulated.
        simulation_time += dt

        previous_center = self._ball_center(ball)

        # Integrate only the ball. Paddle position is deterministic-control.
        ball.position = Vec2(
            x=ball.position.x + ball.velocity.x * dt,
            y=ball.position.y + ball.velocity.y * dt,
        )

        # Virtual top / left / right walls.
        self._collide_virtual_walls(
            ball, screen_width, screen_height, events
        )

        # No bottom wall.  If the ball escapes below the play area, process
        # life loss immediately.
        loss_margin = ball.size.y
        if ball.position.y > screen_height + loss_margin:
            lives = max(0, lives - 1)
            events.append("life_lost")
            if lives > 0:
                game_state = "READY"
                ball_attached = True
                if paddle is not None:
                    self._attach_ball(ball, paddle)
            else:
                game_state = "LOST"
                ball_attached = False

            return self._commit(
                ctx,
                entities=entities,
                game_state=game_state,
                score=score,
                lives=lives,
                ball_attached=ball_attached,
                simulation_time=simulation_time,
                frame_count=frame_count,
                events=events,
            )

        # Paddle collision: reflect vertical velocity when the downward-moving
        # ball's AABB overlaps the paddle AABB.
        if paddle is not None:
            self._reflect_off_paddle(ball, paddle, events)

        # Brick collisions.  Enabled bricks only; disabled bricks are ignored.
        previous_brick_center = previous_center
        for brick in list(entities):
            if getattr(brick, "kind", None) != "brick":
                continue
            if not getattr(brick, "enabled", False):
                continue

            if self._brick_hit(ball, brick, previous_brick_center):
                brick.enabled = False
                score += self._to_int(
                    self._cfg.get("game.points_per_brick", 10), 0
                )
                events.append("brick_hit:{}".format(getattr(brick, "id", "")))

        if not self._has_enabled_brick(entities):
            game_state = "WON"
            events.append("win")

        return self._commit(
            ctx,
            entities=entities,
            game_state=game_state,
            score=score,
            lives=lives,
            ball_attached=ball_attached,
            simulation_time=simulation_time,
            frame_count=frame_count,
            events=events,
        )

    # ------------------------------------------------------------------
    # Scene construction
    # ------------------------------------------------------------------
    def _create_world(self) -> Dict[str, Any]:
        cfg = self._cfg

        screen_width = float(cfg.get("game.screen_width", 800))
        screen_height = float(cfg.get("game.screen_height", 600))
        paddle_width = float(cfg.get("game.paddle_width", 120))
        paddle_height = float(cfg.get("game.paddle_height", 16))
        bottom_margin = float(cfg.get("game.paddle_bottom_margin", 24))
        ball_diameter = float(cfg.get("game.ball_diameter", 14))
        rows = int(cfg.get("game.brick_rows", 6))
        cols = int(cfg.get("game.brick_cols", 10))
        brick_top_offset = float(cfg.get("game.brick_top_offset", 64))
        brick_height = float(cfg.get("game.brick_height", 20))
        brick_horizontal_margin = float(
            cfg.get("game.brick_horizontal_margin", 12)
        )
        brick_gap = float(cfg.get("game.brick_gap", 4))

        base_lives = self._base_lives()

        paddle_x = (screen_width - paddle_width) / 2.0
        paddle_y = screen_height - bottom_margin - paddle_height
        ball_x = paddle_x + (paddle_width - ball_diameter) / 2.0
        ball_y = paddle_y - ball_diameter

        paddle = GameEntity(
            id="paddle",
            kind="paddle",
            position=Vec2(x=paddle_x, y=paddle_y),
            velocity=Vec2(x=0.0, y=0.0),
            size=Vec2(x=paddle_width, y=paddle_height),
            shape="rect",
            enabled=True,
            static=False,
        )

        ball = GameEntity(
            id="ball",
            kind="ball",
            position=Vec2(x=ball_x, y=ball_y),
            velocity=Vec2(x=0.0, y=0.0),
            size=Vec2(x=ball_diameter, y=ball_diameter),
            shape="circle",
            enabled=True,
            static=False,
        )

        entities: List[GameEntity] = [paddle, ball]

        if rows > 0 and cols > 0:
            total_gap_width = (cols - 1) * brick_gap
            available = (
                screen_width
                - 2.0 * brick_horizontal_margin
                - total_gap_width
            )
            brick_width = max(1.0, available / float(cols))
            for row in range(rows):
                for col in range(cols):
                    brick_x = (
                        brick_horizontal_margin
                        + col * (brick_width + brick_gap)
                    )
                    brick_y = brick_top_offset + row * (brick_height + brick_gap)
                    entities.append(
                        GameEntity(
                            id="brick_r{}_c{}".format(row, col),
                            kind="brick",
                            position=Vec2(x=brick_x, y=brick_y),
                            velocity=Vec2(x=0.0, y=0.0),
                            size=Vec2(x=brick_width, y=brick_height),
                            shape="rect",
                            enabled=True,
                            static=True,
                        )
                    )

        return {
            "entities": entities,
            "game_state": "READY",
            "score": 0,
            "lives": base_lives,
            "ball_attached": True,
            "simulation_time": 0.0,
        }

    def _base_lives(self) -> int:
        return max(0, int(self._cfg.get("game.base_lives", 3)))

    # ------------------------------------------------------------------
    # Movement and launch helpers
    # ------------------------------------------------------------------
    def _move_paddle(
        self,
        paddle: Optional[Any],
        left: bool,
        right: bool,
        dt: float,
    ) -> None:
        if paddle is None:
            return

        speed = float(self._cfg.get("game.paddle_speed", 420))
        dx = 0.0
        if left:
            dx -= speed * dt
        if right:
            dx += speed * dt

        screen_width = float(self._cfg.get("game.screen_width", 800))
        paddle_width = float(getattr(paddle, "size", Vec2(x=0.0, y=0.0)).x)
        max_x = max(0.0, screen_width - paddle_width)
        new_x = self._clamp(getattr(paddle.position, "x", 0.0) + dx, 0.0, max_x)
        paddle.position = Vec2(x=new_x, y=paddle.position.y)

    def _attach_ball(self, ball: Any, paddle: Any) -> None:
        if ball is None or paddle is None:
            return
        paddle_center_x = (
            paddle.position.x + paddle.size.x / 2.0
        )
        ball.position = Vec2(
            x=paddle_center_x - ball.size.x / 2.0,
            y=paddle.position.y - ball.size.y,
        )

    def _apply_launch_velocity(self, ball: Any) -> None:
        speed = float(self._cfg.get("game.ball_speed", 330))
        angle_degrees = float(
            self._cfg.get("game.ball_start_angle_deg", 60)
        )
        angle = math.radians(angle_degrees)
        # Deterministic launch: +x and upward -y.
        ball.velocity = Vec2(
            x=speed * math.cos(angle),
            y=-speed * math.sin(angle),
        )

    # ------------------------------------------------------------------
    # Paddle bounce
    # ------------------------------------------------------------------
    def _reflect_off_paddle(
        self, ball: Any, paddle: Any, events: List[str]
    ) -> bool:
        if ball is None or paddle is None:
            return False

        # The spec intentionally uses the ball's AABB against the paddle AABB.
        if ball.velocity.y <= 0.0:
            return False

        ball_right = ball.position.x + ball.size.x
        ball_bottom = ball.position.y + ball.size.y
        paddle_right = paddle.position.x + paddle.size.x
        paddle_bottom = paddle.position.y + paddle.size.y

        if ball_right <= paddle.position.x:
            return False
        if ball.position.x >= paddle_right:
            return False
        if ball_bottom <= paddle.position.y:
            return False
        if ball.position.y >= paddle_bottom:
            return False

        ball_center_x = ball.position.x + ball.size.x / 2.0
        paddle_center_x = paddle.position.x + paddle.size.x / 2.0
        paddle_half_width = paddle.size.x / 2.0
        if paddle_half_width <= 0.0:
            normalized_offset = 0.0
        else:
            normalized_offset = self._clamp(
                (ball_center_x - paddle_center_x) / paddle_half_width,
                -1.0,
                1.0,
            )

        speed = float(self._cfg.get("game.ball_speed", 330))
        max_angle = math.radians(
            float(self._cfg.get("game.ball_start_angle_deg", 60))
        )
        angle = max_angle * normalized_offset

        # angle is measured from vertical; positive offset sends the ball right.
        ball.velocity = Vec2(
            x=speed * math.sin(angle),
            y=-speed * math.cos(angle),
        )

        # Place the ball just above the paddle.
        ball.position = Vec2(
            x=ball.position.x,
            y=paddle.position.y - ball.size.y,
        )

        events.append("paddle_hit")
        return True

    # ------------------------------------------------------------------
    # Virtual wall collision
    # ------------------------------------------------------------------
    def _collide_virtual_walls(
        self,
        ball: Any,
        screen_width: float,
        screen_height: float,
        events: List[str],
    ) -> None:
        # Left virtual wall.
        if ball.position.x <= 0.0 and ball.velocity.x < 0.0:
            ball.position = Vec2(x=-ball.position.x, y=ball.position.y)
            ball.velocity = Vec2(x=abs(ball.velocity.x), y=ball.velocity.y)
            events.append("wall_left")

        # Right virtual wall.
        right_edge = ball.position.x + ball.size.x
        if right_edge >= screen_width and ball.velocity.x > 0.0:
            overshoot = right_edge - screen_width
            ball.position = Vec2(
                x=screen_width - ball.size.x - overshoot,
                y=ball.position.y,
            )
            ball.velocity = Vec2(x=-abs(ball.velocity.x), y=ball.velocity.y)
            events.append("wall_right")

        # Top virtual wall.
        if ball.position.y <= 0.0 and ball.velocity.y < 0.0:
            ball.position = Vec2(x=ball.position.x, y=-ball.position.y)
            ball.velocity = Vec2(x=ball.velocity.x, y=abs(ball.velocity.y))
            events.append("wall_top")

        # screen_height is used only for the loss check elsewhere.  Keeping it
        # in the signature documents that there is intentionally no bottom wall
        # reflection in this method.
        _ = screen_height

    # ------------------------------------------------------------------
    # Brick collision
    # ------------------------------------------------------------------
    def _brick_hit(
        self,
        ball: Any,
        brick: Any,
        previous_center: Optional[Vec2],
    ) -> bool:
        radius = ball.size.x / 2.0
        center = self._ball_center(ball)

        brick_x = brick.position.x
        brick_y = brick.position.y
        brick_w = brick.size.x
        brick_h = brick.size.y

        closest_x = self._clamp(center.x, brick_x, brick_x + brick_w)
        closest_y = self._clamp(center.y, brick_y, brick_y + brick_h)
        delta_x = center.x - closest_x
        delta_y = center.y - closest_y
        distance_sq = delta_x * delta_x + delta_y * delta_y

        if distance_sq > radius * radius:
            return False

        normal_x = 0.0
        normal_y = 0.0

        if distance_sq > 1e-9:
            dist = math.sqrt(distance_sq)
            normal_x = delta_x / dist
            normal_y = delta_y / dist
        else:
            # Ball center is inside the brick.  Prefer the outward normal from
            # the previous center; fall back to nearest-face penetration.
            if previous_center is not None:
                old_closest_x = self._clamp(
                    previous_center.x, brick_x, brick_x + brick_w
                )
                old_closest_y = self._clamp(
                    previous_center.y, brick_y, brick_y + brick_h
                )
                old_dx = previous_center.x - old_closest_x
                old_dy = previous_center.y - old_closest_y
                old_distance_sq = old_dx * old_dx + old_dy * old_dy
                if old_distance_sq > 1e-9:
                    old_dist = math.sqrt(old_distance_sq)
                    normal_x = old_dx / old_dist
                    normal_y = old_dy / old_dist
                else:
                    normal_x, normal_y = self._inside_brick_normal(
                        center, brick
                    )
            else:
                normal_x, normal_y = self._inside_brick_normal(center, brick)

        self._reflect_with_normal(ball, normal_x, normal_y)
        return True

    def _inside_brick_normal(
        self, center: Vec2, brick: Any
    ) -> Tuple[float, float]:
        brick_x = brick.position.x
        brick_y = brick.position.y
        brick_w = brick.size.x
        brick_h = brick.size.y

        top = center.y - brick_y
        bottom = brick_y + brick_h - center.y
        left = center.x - brick_x
        right = brick_x + brick_w - center.x

        # Pick the nearest face; ties are resolved deterministically.
        if top <= bottom and top <= left and top <= right:
            return (0.0, -1.0)
        if bottom <= left and bottom <= right:
            return (0.0, 1.0)
        if left <= right:
            return (-1.0, 0.0)
        return (1.0, 0.0)

    def _reflect_with_normal(
        self, ball: Any, normal_x: float, normal_y: float
    ) -> None:
        dot = ball.velocity.x * normal_x + ball.velocity.y * normal_y
        if dot < 0.0:
            ball.velocity = Vec2(
                ball.velocity.x - 2.0 * dot * normal_x,
                ball.velocity.y - 2.0 * dot * normal_y,
            )

    # ------------------------------------------------------------------
    # Context/value helpers
    # ------------------------------------------------------------------
    def _commit(
        self,
        ctx: PipelineContext,
        *,
        entities: Any,
        game_state: str,
        score: int,
        lives: int,
        ball_attached: bool,
        simulation_time: float,
        frame_count: int,
        events: List[str],
    ) -> dict:
        clean_events = [str(event) for event in (events or [])]

        ctx.set("entities", entities)
        ctx.set("game_state", game_state)
        ctx.set("score", score)
        ctx.set("lives", lives)
        ctx.set("ball_attached", ball_attached)
        ctx.set("previous_commands", self._normalize_commands(None))
        ctx.set("simulation_time", simulation_time)
        ctx.set("frame_count", frame_count)
        ctx.set("last_frame_events", clean_events)

        return {
            "entities": entities,
            "game_state": game_state,
            "score": score,
            "lives": lives,
            "ball_attached": ball_attached,
            "previous_commands": self._normalize_commands(None),
            "simulation_time": simulation_time,
            "frame_count": frame_count,
            "last_frame_events": clean_events,
        }

    def _resolve_dt(self, ctx: PipelineContext) -> float:
        raw_dt = self._safe_get(ctx, "dt")
        if raw_dt is None:
            raw_dt = self._cfg.get(
                "game.fixed_timestep", 1.0 / 60.0
            )
        try:
            dt = float(raw_dt)
        except (TypeError, ValueError):
            dt = float(self._cfg.get("game.fixed_timestep", 1.0 / 60.0))
        return max(0.0, dt)

    @staticmethod
    def _safe_get(ctx: PipelineContext, key: str) -> Any:
        getter = getattr(ctx, "get", None)
        if getter is not None:
            try:
                value = getter(key)
                if value is not None:
                    return value
            except Exception:
                pass

        params = getattr(ctx, "params", None)
        if params is None:
            return None

        if isinstance(params, dict):
            return params.get(key)

        params_get = getattr(params, "get", None)
        if params_get is not None:
            try:
                if key in params:
                    return params_get(key)
            except Exception:
                pass

        return getattr(params, key, None)

    def _normalize_commands(self, raw: Any) -> Dict[str, bool]:
        result: Dict[str, bool] = {
            key: False for key in self._COMMAND_KEYS
        }
        if not isinstance(raw, dict):
            return result

        for key in self._COMMAND_KEYS:
            result[key] = self._to_bool(raw.get(key), False)
        return result

    @staticmethod
    def _normalize_state(raw: Any) -> str:
        if raw in BreakoutSimulationService._VALID_STATES:
            return raw
        return "READY"

    @staticmethod
    def _to_bool(value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    @staticmethod
    def _to_int(value: Any, default: int) -> int:
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_float(value: Any, default: float) -> float:
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

    @staticmethod
    def _ball_center(ball: Any) -> Vec2:
        return Vec2(
            x=ball.position.x + ball.size.x / 2.0,
            y=ball.position.y + ball.size.y / 2.0,
        )

    @staticmethod
    def _find_entity(entities: Any, entity_id: str) -> Any:
        for entity in entities or []:
            if isinstance(entity, dict):
                if entity.get("id") == entity_id:
                    return entity
            elif getattr(entity, "id", None) == entity_id:
                return entity
        return None

    @staticmethod
    def _has_enabled_brick(entities: Any) -> bool:
        for entity in entities or []:
            if getattr(entity, "kind", None) != "brick":
                continue
            if getattr(entity, "enabled", False):
                return True
        return False

    @classmethod
    def _coerce_entities(cls, raw_entities: Any) -> List[Any]:
        result: List[Any] = []
        for item in raw_entities or []:
            if isinstance(item, GameEntity):
                result.append(item)
                continue
            if isinstance(item, dict):
                result.append(
                    GameEntity(
                        id=str(item.get("id", "")),
                        kind=str(item.get("kind", "")),
                        position=cls._coerce_vec2(item.get("position")),
                        velocity=cls._coerce_vec2(item.get("velocity")),
                        size=cls._coerce_vec2(item.get("size")),
                        shape=str(item.get("shape", "rect")),
                        enabled=cls._to_bool(item.get("enabled"), True),
                        static=cls._to_bool(item.get("static"), False),
                    )
                )
                continue
            result.append(item)
        return result

    @staticmethod
    def _coerce_vec2(value: Any) -> Vec2:
        if isinstance(value, Vec2):
            return value
        if isinstance(value, dict):
            return Vec2(
                x=float(value.get("x", 0.0)),
                y=float(value.get("y", 0.0)),
            )
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return Vec2(x=float(value[0]), y=float(value[1]))
        return Vec2(x=0.0, y=0.0)
