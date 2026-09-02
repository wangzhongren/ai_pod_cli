import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";

type Database = Record<string, Record<string, Record<string, unknown>>>;

export class ModelRepository {
  readonly #path: string;
  #queue: Promise<unknown> = Promise.resolve();

  constructor(projectRoot: string, file = ".aipod/data.json") {
    this.#path = resolve(projectRoot, file);
  }

  async #read(): Promise<Database> {
    try {
      return JSON.parse(await readFile(this.#path, "utf8")) as Database;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
      throw error;
    }
  }

  async #write(database: Database): Promise<void> {
    await mkdir(dirname(this.#path), { recursive: true });
    const temporary = `${this.#path}.tmp`;
    await writeFile(temporary, `${JSON.stringify(database, null, 2)}\n`);
    await rename(temporary, this.#path);
  }

  async save<T extends Record<string, unknown>>(
    collection: string,
    value: T,
    idField = "id",
  ): Promise<T> {
    const id = value[idField];
    if (typeof id !== "string" && typeof id !== "number") {
      throw new Error(`ModelRepository requires '${idField}' as string or number`);
    }
    return this.#serialize(async () => {
      const database = await this.#read();
      database[collection] ??= {};
      database[collection]![String(id)] = structuredClone(value);
      await this.#write(database);
      return structuredClone(value);
    });
  }

  async get<T extends Record<string, unknown>>(collection: string, id: string | number): Promise<T | undefined> {
    const database = await this.#read();
    const value = database[collection]?.[String(id)];
    return value ? structuredClone(value) as T : undefined;
  }

  async list<T extends Record<string, unknown>>(collection: string): Promise<T[]> {
    const database = await this.#read();
    return Object.values(database[collection] ?? {}).map((value) => structuredClone(value) as T);
  }

  async find<T extends Record<string, unknown>>(
    collection: string,
    filters: Record<string, unknown>,
  ): Promise<T[]> {
    return (await this.list<T>(collection)).filter((value) =>
      Object.entries(filters).every(([key, expected]) => Object.is(value[key], expected))
    );
  }

  async delete(collection: string, id: string | number): Promise<boolean> {
    return this.#serialize(async () => {
      const database = await this.#read();
      const values = database[collection];
      if (!values || !(String(id) in values)) return false;
      delete values[String(id)];
      await this.#write(database);
      return true;
    });
  }

  #serialize<T>(operation: () => Promise<T>): Promise<T> {
    const next = this.#queue.then(operation, operation);
    this.#queue = next.then(() => undefined, () => undefined);
    return next;
  }
}
