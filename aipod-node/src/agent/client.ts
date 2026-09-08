import type { ModelClient } from "./types.js";

export const DEFAULT_JSON_MAX_TOKENS = 32_768;
export const DEFAULT_SOURCE_MAX_TOKENS = 65_536;
export const DEFAULT_MODEL_TIMEOUT_MS = 600_000;

function parseJson(content: string): Record<string, unknown> {
  const trimmed = content.trim();
  try {
    return JSON.parse(trimmed) as Record<string, unknown>;
  } catch {
    const fenced = trimmed.match(/```(?:json)?\s*([\s\S]*?)```/i)?.[1];
    if (fenced) return JSON.parse(fenced) as Record<string, unknown>;
    const start = trimmed.indexOf("{");
    const end = trimmed.lastIndexOf("}");
    if (start >= 0 && end > start) return JSON.parse(trimmed.slice(start, end + 1)) as Record<string, unknown>;
    throw new Error("Model response is not a JSON object");
  }
}

export class OpenAICompatibleClient implements ModelClient {
  constructor(
    readonly options: {
      apiKey: string;
      model: string;
      baseUrl?: string;
      timeoutMs?: number;
      jsonMaxTokens?: number;
      sourceMaxTokens?: number;
    },
  ) {}

  async complete(system: string, user: string): Promise<Record<string, unknown>> {
    return this.completeJson(system, user);
  }

  async completeJson(system: string, user: string): Promise<Record<string, unknown>> {
    return parseJson(await this.#request(system, user, true));
  }

  async completeText(system: string, user: string): Promise<string> {
    return this.#request(system, user, false);
  }

  async #request(system: string, user: string, jsonMode: boolean): Promise<string> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.options.timeoutMs ?? DEFAULT_MODEL_TIMEOUT_MS);
    try {
      const base = (this.options.baseUrl ?? "https://api.openai.com/v1").replace(/\/$/, "");
      const response = await fetch(`${base}/chat/completions`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          authorization: `Bearer ${this.options.apiKey}`,
        },
        body: JSON.stringify({
          model: this.options.model,
          max_tokens: jsonMode
            ? this.options.jsonMaxTokens ?? DEFAULT_JSON_MAX_TOKENS
            : this.options.sourceMaxTokens ?? DEFAULT_SOURCE_MAX_TOKENS,
          messages: [
            // JSON-mode endpoints can reject requests before generation unless
            // the messages explicitly request JSON, even if they show a schema.
            { role: "system", content: jsonMode ? `${system}\nReturn a strict JSON object.` : system },
            { role: "user", content: user },
          ],
          ...(jsonMode ? { response_format: { type: "json_object" } } : {}),
          temperature: 0.1,
        }),
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`Model request failed (${response.status}): ${await response.text()}`);
      const payload = await response.json() as {
        choices?: { message?: { content?: string }; finish_reason?: string }[];
      };
      if (payload.choices?.[0]?.finish_reason === "length") {
        throw new Error("Model response was truncated (finish_reason=length)");
      }
      const content = payload.choices?.[0]?.message?.content;
      if (!content) throw new Error("Model response has no content");
      return content;
    } finally {
      clearTimeout(timeout);
    }
  }
}
