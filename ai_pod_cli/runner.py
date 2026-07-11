"""PipelineRunner — load and execute pipeline files by route name."""

import importlib.util
import os

import tomlkit

from ai_pod_cli.config import ROUTES_TOML


class PipelineRunner:
    """Loads routes.toml and dispatches to the correct pipeline.

    Usage:
        runner = PipelineRunner()
        result = runner.run("stock_check", {"sku_id": 1001})
    """

    def __init__(self, routes_path: str = ROUTES_TOML):
        self._routes_path = routes_path
        self._routes: dict = {}
        self._load()

    def _load(self):
        if os.path.exists(self._routes_path):
            with open(self._routes_path, "r", encoding="utf-8") as f:
                self._routes = tomlkit.load(f)

    def routes(self) -> dict:
        """Return all registered routes."""
        return dict(self._routes)

    def route_names(self) -> list[str]:
        """Return all route names."""
        return list(self._routes.keys())

    def get_route(self, name: str) -> dict | None:
        """Get route config by name."""
        return self._routes.get(name)

    def run(self, route_name: str, params: dict | None = None) -> dict:
        """Load the pipeline for the given route and execute run(ctx).

        Args:
            route_name: The route name defined in routes.toml.
            params: Parameters to pass into PipelineContext.params.

        Returns:
            The result returned by the pipeline's run(ctx) function.

        Raises:
            KeyError: If the route name is not found.
            FileNotFoundError: If the pipeline file does not exist.
        """
        from ai_pod_cli.context import PipelineContext

        route = self._routes.get(route_name)
        if route is None:
            raise KeyError(
                f"Route '{route_name}' not found in {self._routes_path}. "
                f"Available: {list(self._routes.keys())}"
            )

        pipeline_path = route.get("pipeline", "")
        if not os.path.exists(pipeline_path):
            raise FileNotFoundError(f"Pipeline file not found: {pipeline_path}")

        # Load the pipeline module
        abs_path = os.path.abspath(pipeline_path)
        module_name = os.path.splitext(os.path.basename(abs_path))[0]

        spec = importlib.util.spec_from_file_location(
            f"pipeline.{module_name}", abs_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        if not hasattr(module, "run"):
            raise AttributeError(
                f"Pipeline {pipeline_path} does not define a run() function"
            )

        # Execute run(ctx)
        ctx = PipelineContext(params=params or {})
        result = module.run(ctx)
        return result or ctx.summary()
