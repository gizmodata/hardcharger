import { Binary, Field, Int32, Schema, Struct, Table, tableFromIPC, tableToIPC, vectorFromArray } from "apache-arrow";
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { toDelimited, toJson } from "../../src/lib/export";
import { columnText, jsonFor, textFor, typeLabel } from "../../src/lib/format";
import { variantValue } from "../../src/lib/variant";
import { sheetXml } from "../../src/lib/xlsx";

interface Example {
  name: string;
  metadata: string;
  value: string;
  text: string;
  json: string;
}
const examples = JSON.parse(
  readFileSync(path.join(import.meta.dirname, "../../../server/client/tests/data/variant.json"), "utf8"),
) as Example[];
const bytes = (hex: string) => Uint8Array.from(hex.match(/../g) ?? [], (b) => parseInt(b, 16));
const storage = new Struct([new Field("metadata", new Binary()), new Field("value", new Binary())]);
const field = new Field("v", storage, true, new Map([["ARROW:extension:name", "arrow.parquet.variant"]]));
const row = (example: Example) => ({ metadata: bytes(example.metadata), value: bytes(example.value) });

describe("Parquet VARIANT v1 (shared with the CLI)", () => {
  for (const example of examples)
    it(example.name, () => {
      expect(variantValue(row(example))).toEqual({ text: example.text, json: example.json });
      expect(textFor(field)(row(example))).toBe(example.text);
      expect(jsonFor(field)(row(example))).toBe(example.json);
    });

  it("requires the extension metadata, including for nested fields", () => {
    const ordinary = new Field("v", storage);
    const value = row(examples[0]!);
    expect(typeLabel(field)).toBe("VARIANT");
    expect(typeLabel(ordinary)).toMatch(/^STRUCT/);
    expect(textFor(ordinary)(value)).toContain("metadata");
    const parent = new Struct([field, new Field("n", new Int32())]);
    expect(jsonFor(parent)({ v: value, n: 7 })).toBe('{"v":42,"n":7}');
    expect(textFor(field)(null)).toBeNull();
    expect(jsonFor(field)(null)).toBe("null");
  });

  it("copies and exports decoded values while Arrow preserves storage and metadata", () => {
    const values = [row(examples[0]!), row(examples.find((e) => e.name === "object unordered storage")!), null];
    const table = new Table(new Schema([field]), { v: vectorFromArray(values, storage) });
    expect(columnText(table.schema)[0]!(table.getChildAt(0)!.get(0))).toBe("42");
    expect(toDelimited(table, "\t")).toContain("42\n");
    expect(toJson(table)).toContain('{"v":{"a":42,"b":[true,null,false]}}');
    expect(sheetXml(table)).toContain("42</t>");
    const restored = tableFromIPC(tableToIPC(table));
    expect(restored.schema.fields[0]!.metadata.get("ARROW:extension:name")).toBe("arrow.parquet.variant");
    expect(restored.getChildAt(0)!.get(0).value).toEqual(bytes(examples[0]!.value));
  });

  it("bounds malformed lengths and unsupported shredded storage", () => {
    expect(variantValue({ metadata: bytes("010000"), value: bytes("0300ff") }).text).toContain("VARIANT:");
    expect(variantValue({ ...row(examples[0]!), typed_value: 2 }).text).toContain("unsupported VARIANT storage");
  });
});
