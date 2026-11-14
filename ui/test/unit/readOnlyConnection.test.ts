import type { Connection } from "@gizmodata/chargewire";
import { expect, it, vi } from "vitest";
import { confirmReadOnly } from "../../src/lib/chargewire";

it.each([false, undefined, "true", 1])(
  "refuses unconfirmed read-only mode (%s) and closes the session",
  async (value) => {
    const close = vi.fn(async () => {});
    const conn = { getSession: async () => ({ options: { read_only: value } }), close } as unknown as Connection;
    await expect(confirmReadOnly(conn, true)).rejects.toThrow("did not confirm");
    expect(close).toHaveBeenCalledOnce();
  },
);

it("accepts explicit confirmation, while ordinary connections stay lazy", async () => {
  const getSession = vi.fn(async () => ({ options: { read_only: true } }));
  const close = vi.fn(async () => {});
  const conn = { getSession, close } as unknown as Connection;
  expect(await confirmReadOnly(conn, false)).toBe(conn);
  expect(getSession).not.toHaveBeenCalled();
  expect(await confirmReadOnly(conn, true)).toBe(conn);
  expect(getSession).toHaveBeenCalledOnce();
  expect(close).not.toHaveBeenCalled();
});
