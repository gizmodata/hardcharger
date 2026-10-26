import { parseDocument, stringify } from "yaml";

export interface ServerOption {
  name: string; yaml_key: string; group: string; description: string;
  type: "string" | "integer" | "boolean" | "list";
  default: string; managed: boolean; secret: boolean;
  choices?: string[]; choices_ignore_case?: boolean;
}

export function optionChoices(option: ServerOption): string[] | undefined {
  return option.type === "boolean" ? ["true", "false"] : option.choices;
}

export function choiceValue(option: ServerOption, value: string): string | undefined {
  return optionChoices(option)?.find(choice => option.choices_ignore_case
    ? choice.toLowerCase() === value.toLowerCase() : choice === value);
}

export function parseConfiguration(text: string, schema: ServerOption[]): Record<string, unknown> {
  if (text.length > 1024 * 1024) throw new Error("Configuration files must be smaller than 1 MB.");
  const document = parseDocument(text, { uniqueKeys: true });
  if (document.errors.length) throw new Error(document.errors[0].message);
  const values: unknown = document.toJS({ maxAliasCount: 100 });
  if (!values || typeof values !== "object" || Array.isArray(values)) throw new Error("The YAML must contain a mapping of server option names to values.");
  const options = values as Record<string, unknown>;
  for (const [key, value] of Object.entries(options)) {
    const definition = schema.find(o => o.name === key);
    if (!definition) throw new Error(`Unknown server option: ${key}`);
    if (definition.managed) throw new Error(`${key} is managed by the desktop app. Remove it from the imported YAML.`);
    const valid = definition.type === "integer" ? Number.isSafeInteger(value)
      : definition.type === "list" ? Array.isArray(value) && value.every(v => typeof v === "string")
      : typeof value === definition.type;
    if (!valid) throw new Error(`${key} must have type ${definition.type}.`);
    if (optionChoices(definition)?.length && choiceValue(definition, String(value)) === undefined)
      throw new Error(`${key} must be one of: ${optionChoices(definition)!.join(", ")}.`);
    if (key === "port" && (Number(value) < 1024 || Number(value) > 65535)) throw new Error("Choose a port between 1024 and 65535.");
  }
  return options;
}

export function exportConfiguration(port: number, options: Record<string, unknown>, schema: ServerOption[]): string {
  const values = Object.fromEntries(Object.entries(options).filter(([key]) => {
    const definition = schema.find(o => o.name === key);
    return definition && !definition.secret && !definition.managed;
  }));
  return "# Hardcharger server configuration\n# Passwords and other secrets are omitted.\n# Desktop-managed networking and authentication are not exported.\n" + stringify({ port, ...values });
}
