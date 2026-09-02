import { PipelineContext } from "./context.js";
import type { ExecutionNode } from "./pipeline.js";
import type { Result } from "./result.js";

export interface RouteDefinition {
  name: string;
  pipeline: ExecutionNode;
  description?: string;
}

export class PipelineRunner {
  readonly #routes: Map<string, RouteDefinition>;

  constructor(routes: RouteDefinition[]) {
    this.#routes = new Map(routes.map((route) => [route.name, route]));
    if (this.#routes.size !== routes.length) throw new Error("Route names must be unique");
  }

  routeNames(): string[] {
    return [...this.#routes.keys()];
  }

  async run(
    routeName: string,
    params: Record<string, unknown> = {},
  ): Promise<{ result: Result; context: PipelineContext }> {
    const route = this.#routes.get(routeName);
    if (!route) throw new Error(`Unknown route '${routeName}'`);
    const context = new PipelineContext(params);
    const result = await route.pipeline.execute(context);
    return { result, context };
  }
}
