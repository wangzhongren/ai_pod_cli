export type ContractType = "string" | "number" | "integer" | "boolean" | "object" | "array" | "any";

export interface ContractField {
  type?: ContractType;
  required?: boolean;
  properties?: Contract;
  items?: ContractField;
}

export type Contract = Record<string, ContractField>;

export function validateContractValue(value: unknown, field: ContractField, path: string): string[] {
  const type = field.type ?? "any";
  const valid = type === "any" ||
    (type === "string" && typeof value === "string") ||
    (type === "number" && typeof value === "number" && Number.isFinite(value)) ||
    (type === "integer" && Number.isInteger(value)) ||
    (type === "boolean" && typeof value === "boolean") ||
    (type === "object" && typeof value === "object" && value !== null && !Array.isArray(value)) ||
    (type === "array" && Array.isArray(value));
  if (!valid) return [`${path}: expected ${type}`];
  if (type === "object" && field.properties && typeof value === "object" && value !== null) {
    return validateContract(value as Record<string, unknown>, field.properties, path);
  }
  if (type === "array" && field.items && Array.isArray(value)) {
    return value.flatMap((item, index) => validateContractValue(item, field.items!, `${path}[${index}]`));
  }
  return [];
}

export function validateContract(
  data: Record<string, unknown>,
  contract: Contract = {},
  prefix = "$",
): string[] {
  const errors: string[] = [];
  for (const [name, field] of Object.entries(contract)) {
    if (!(name in data)) {
      if (field.required ?? true) errors.push(`${prefix}.${name}: required field is missing`);
      continue;
    }
    errors.push(...validateContractValue(data[name], field, `${prefix}.${name}`));
  }
  return errors;
}

export function validateServiceSource(source: string): string[] {
  const errors: string[] = [];
  if (/from\s+["'][^"']*services[^"']*["']|require\(["'][^"']*services/.test(source)) {
    errors.push("Service cannot import another Service; compose them in a Pipeline");
  }
  if (/PipelineRunner|runRoute\s*\(/.test(source)) {
    errors.push("Service cannot access route/runtime orchestration");
  }
  return errors;
}

export interface ContractComponent {
  id: string;
  inputs: Contract;
  outputs: Contract;
}

export interface PipelineContractAnalysis {
  valid: boolean;
  inputs: Contract;
  outputs: Contract;
  issues: { code: string; component: string; field: string; message: string }[];
  warnings: { code: string; component: string; field: string; producedField: string }[];
}

const compatible = (produced: ContractField, required: ContractField) => {
  const left = produced.type ?? "any";
  const right = required.type ?? "any";
  return left === "any" || right === "any" || left === right || (left === "integer" && right === "number");
};

const tokens = (value: string) => value
  .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
  .toLowerCase()
  .split(/[^a-z0-9]+/)
  .filter((token) => token && !["value", "data", "info", "result", "current"].includes(token))
  .map((token) => token.endsWith("s") && token.length > 3 ? token.slice(0, -1) : token);

const semanticallySimilar = (left: string, right: string) => {
  const a = tokens(left);
  const b = tokens(right);
  if (a.join("_") === b.join("_") && a.length) return true;
  const aSet = new Set(a);
  const overlap = b.filter((token) => aSet.has(token)).length;
  return overlap > 0 && (2 * overlap) / (a.length + b.length) >= 0.8;
};

export function analyzePipelineContracts(
  serviceIds: string[],
  components: ContractComponent[],
): PipelineContractAnalysis {
  const byId = new Map(components.map((component) => [component.id, component]));
  const available: Contract = {};
  const external: Contract = {};
  const issues: PipelineContractAnalysis["issues"] = [];
  const warnings: PipelineContractAnalysis["warnings"] = [];
  serviceIds.forEach((id, index) => {
    const component = byId.get(id);
    if (!component) return;
    for (const [field, required] of Object.entries(component.inputs ?? {})) {
      const produced = available[field];
      if (!produced) {
        const candidate = Object.entries(available).find(([name, spec]) =>
          compatible(spec, required) && semanticallySimilar(name, field)
        );
        if (index > 0 && candidate) {
          warnings.push({
            code: "semantic_field_drift", component: id, field,
            producedField: candidate[0],
          });
        } else if (required.required ?? true) {
          external[field] ??= required;
        }
      } else if (!compatible(produced, required)) {
        issues.push({
          code: "contract_type_mismatch", component: id, field,
          message: `${id}.${field} requires ${required.type ?? "any"}, upstream provides ${produced.type ?? "any"}`,
        });
      }
    }
    Object.assign(available, component.outputs ?? {});
  });
  return { valid: issues.length === 0, inputs: external, outputs: available, issues, warnings };
}
