import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { parse } from "smol-toml";

import { loadProjectConfiguration } from "./shared-config.js";

export class ConfigStore {
  #values: Record<string, unknown> = {};

  constructor(readonly projectRoot: string, readonly file?: string) {}

  async load(): Promise<this> {
    if (!this.file) {
      this.#values = await loadProjectConfiguration(this.projectRoot);
      return this;
    }
    try {
      const content = await readFile(resolve(this.projectRoot, this.file), "utf8");
      this.#values = this.file.endsWith(".toml")
        ? parse(content) as Record<string, unknown>
        : JSON.parse(content) as Record<string, unknown>;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      this.#values = {};
    }
    return this;
  }

  get<T = unknown>(path: string, fallback?: T): T | unknown {
    let current: unknown = this.#values;
    for (const part of path.split(".")) {
      if (typeof current !== "object" || current === null || !(part in current)) return fallback;
      current = (current as Record<string, unknown>)[part];
    }
    return current;
  }

  values(): Record<string, unknown> {
    return structuredClone(this.#values);
  }

  async reload(): Promise<this> {
    return this.load();
  }

  section(name: string): Record<string, unknown> {
    const value = this.#values[name];
    return typeof value === "object" && value !== null && !Array.isArray(value)
      ? structuredClone(value as Record<string, unknown>)
      : {};
  }

  sections(): string[] {
    return Object.keys(this.#values).filter((name) =>
      typeof this.#values[name] === "object" && this.#values[name] !== null
    );
  }
}
