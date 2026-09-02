export interface Effect {
  kind: string;
  target?: string;
  operation?: string;
  metadata?: Record<string, unknown>;
}

export interface Success<T extends Record<string, unknown> = Record<string, unknown>> {
  status: "success";
  output: T;
  effects: Effect[];
}

export interface Failure {
  status: "failure";
  error: {
    code: string;
    message: string;
    retryable: boolean;
    details: Record<string, unknown>;
  };
  effects: Effect[];
}

export type Result<T extends Record<string, unknown> = Record<string, unknown>> =
  | Success<T>
  | Failure;

export function success<T extends Record<string, unknown> = Record<string, unknown>>(
  output = {} as T,
  effects: Effect[] = [],
): Success<T> {
  return { status: "success", output, effects };
}

export function failure(
  message: string,
  options: {
    code?: string;
    retryable?: boolean;
    details?: Record<string, unknown>;
    effects?: Effect[];
  } = {},
): Failure {
  return {
    status: "failure",
    error: {
      code: options.code ?? "component_error",
      message,
      retryable: options.retryable ?? false,
      details: options.details ?? {},
    },
    effects: options.effects ?? [],
  };
}

export function normalizeResult(value: unknown): Result {
  if (
    typeof value === "object" && value !== null &&
    ((value as Result).status === "success" || (value as Result).status === "failure")
  ) {
    return value as Result;
  }
  if (value === undefined || value === null) return success();
  if (typeof value === "object" && !Array.isArray(value)) {
    return success(value as Record<string, unknown>);
  }
  return success({ value });
}
