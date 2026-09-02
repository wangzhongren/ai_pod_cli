import type { Contract } from "./contracts.js";
import type { PipelineContext } from "./context.js";

export type BeanCategory = "model" | "provider" | "service";

export interface Service {
  execute(context: PipelineContext): unknown | Promise<unknown>;
}

export interface BeanDefinition {
  id: string;
  category: BeanCategory;
  dependencies?: string[];
  inputs?: Contract;
  outputs?: Contract;
  factory(dependencies: Readonly<Record<string, unknown>>): unknown;
}

export class Container {
  readonly #definitions: Map<string, BeanDefinition>;
  readonly #instances = new Map<string, unknown>();

  constructor(definitions: BeanDefinition[]) {
    this.#definitions = new Map(definitions.map((definition) => [definition.id, definition]));
    if (this.#definitions.size !== definitions.length) {
      throw new Error("Bean IDs must be unique");
    }
    for (const definition of definitions) {
      if (definition.category !== "service") continue;
      const hidden = (definition.dependencies ?? []).filter((dependency) => {
        const target = this.#definitions.get(dependency);
        return target?.category === "service" || dependency === "PipelineRunner";
      });
      if (hidden.length) {
        throw new Error(
          `Service '${definition.id}' cannot depend on hidden orchestration capabilities: ${hidden.join(", ")}`,
        );
      }
    }
  }

  definition(id: string): BeanDefinition {
    const definition = this.#definitions.get(id);
    if (!definition) throw new Error(`Unknown Bean '${id}'`);
    return definition;
  }

  resolve<T = unknown>(id: string): T {
    if (this.#instances.has(id)) return this.#instances.get(id) as T;
    const definition = this.definition(id);
    if (definition.category === "model") {
      throw new Error(`Model '${id}' is a data type and cannot be injected`);
    }
    const dependencies = Object.fromEntries(
      (definition.dependencies ?? []).map((dependency) => [dependency, this.resolve(dependency)]),
    );
    const instance = definition.factory(dependencies);
    this.#instances.set(id, instance);
    return instance as T;
  }
}
