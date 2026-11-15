// A tab's Read-only switch through the real client (the vendored @gizmodata/chargewire) against
// a stand-in server answering the way Hardcharger does (Chargewire's Api::UpdateSession): a
// refusal is a 200 whose `errors.read_only` carries the code and the server's words, and the
// session is left as it was. The client throws that as a ChargewireError (status 400, the
// option's code); the tab must keep the session. A request that gets no answer is an unknown
// outcome; the tab stops using the session.
import { connect } from "@gizmodata/chargewire";
import { describe, expect, it } from "vitest";

import { ReadOnlyRefusedError, TabSession } from "../../src/lib/tabSession";

const sessions = "/chargewire/v1/sessions";
const sequencesDetail = "read_only cannot be set while the session has temporary sequences: drop them first";

interface Sent {
  method: string;
  path: string;
  body: unknown;
}

function server() {
  const sent: Sent[] = [];
  let count = 0;
  let current = "";
  let readOnly = false;
  let temporarySequences = 0;
  let dropPatches = false;
  const answer = (status: number, body: unknown) =>
    new Response(status === 204 ? null : JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  const doc = () => ({
    session_id: current,
    options: { catalog: "memory", schema: "main", read_only: readOnly },
    transaction: "none",
  });
  const fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const request = new Request(input, init);
    const path = new URL(request.url).pathname;
    const text = request.method === "GET" || request.method === "DELETE" ? "" : await request.text();
    const body: unknown = text ? JSON.parse(text) : undefined;
    sent.push({ method: request.method, path, body });
    if (request.method === "GET" && path === "/chargewire") {
      return answer(200, {
        protocol: "chargewire",
        protocol_versions: ["1"],
        server: { name: "stand-in", version: "0" },
        capabilities: ["transactions", "read_only_sessions"],
        auth: { grant_types: ["password"] },
      });
    }
    if (request.method === "POST" && path === sessions) {
      current = `s_${++count}`;
      readOnly = (body as { options?: { read_only?: unknown } }).options?.read_only === true;
      temporarySequences = 0;
      return answer(201, doc());
    }
    if (path === `${sessions}/${current}`) {
      if (request.method === "GET") return answer(200, doc());
      if (request.method === "DELETE") return answer(204, null);
      if (request.method === "PATCH") {
        // What a browser's fetch throws for a dropped connection (the client retries an
        // idempotent request, so every attempt fails).
        if (dropPatches) throw new TypeError("Failed to fetch");
        const asked = (body as { options: Record<string, unknown> }).options;
        const errors: Record<string, unknown> = {};
        if (asked["read_only"] === true && !readOnly) {
          if (temporarySequences > 0) errors["read_only"] = { code: "invalid_option_value", detail: sequencesDetail };
          else readOnly = true;
        }
        return answer(200, { options: doc().options, errors });
      }
    }
    if (request.method === "POST" && path === `${sessions}/${current}/statements`) {
      const sql = (body as { sql: string }).sql;
      if (sql.startsWith("CREATE TEMP SEQUENCE")) temporarySequences++;
      if (sql.startsWith("DROP SEQUENCE")) temporarySequences--;
      return answer(200, { statement_id: "q_1", state: "succeeded", result: { kind: "update", rows_affected: 0 } });
    }
    return answer(404, { code: "not_found", status: 404 });
  };
  return {
    sent,
    fetch,
    dropPatches: () => {
      dropPatches = true;
    },
    /** Sessions ended (statements' clean-up DELETEs are not counted). */
    deletes: () => sent.filter((s) => s.method === "DELETE" && /^\/chargewire\/v1\/sessions\/[^/]+$/.test(s.path)),
    created: () => sent.filter((s) => s.method === "POST" && s.path === sessions),
  };
}

function tabOn(fetch: typeof globalThis.fetch): TabSession {
  return new TabSession((where) =>
    connect({
      url: "http://db.test:31337/chargewire",
      fetch,
      token: "t",
      reconnect: false,
      ...(where.readOnly ? { sessionOptions: { read_only: true } } : {}),
      resultPartitions: 1,
      logger: { warn: () => {} },
    }),
  );
}

const here = { catalog: "memory", schema: "main" };

describe("Read-only through the real client", () => {
  it("the server's refusal (200, errors.read_only) keeps the session; after the advice, the switch locks it", async () => {
    const standIn = server();
    const tab = tabOn(standIn.fetch);
    await tab.run(here, (c) => c.execute("CREATE TEMP SEQUENCE seq"));
    await tab.refresh(here);
    const err = await tab.enableReadOnly(here).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ReadOnlyRefusedError);
    expect((err as ReadOnlyRefusedError).code).toBe("invalid_option_value");
    expect((err as ReadOnlyRefusedError).detail).toBe(sequencesDetail);
    expect(standIn.deletes()).toEqual([]); // the session was not ended
    expect(tab.info).toMatchObject({ id: "s_1", readOnly: false });

    await tab.run(here, (c) => c.execute("DROP SEQUENCE seq"));
    expect(await tab.enableReadOnly(here)).toMatchObject({ id: "s_1", readOnly: true });
    expect(standIn.created()).toHaveLength(1);
    expect(standIn.deletes()).toEqual([]);
  });

  it("a switch that gets no answer: the session is ended and the tab blocked until New session", async () => {
    const standIn = server();
    const tab = tabOn(standIn.fetch);
    await tab.run(here, (c) => c.execute("SELECT 1"));
    standIn.dropPatches();
    const err = await tab.enableReadOnly(here).catch((e: unknown) => e);
    expect(err).not.toBeInstanceOf(ReadOnlyRefusedError);
    expect((err as Error).message).toMatch(/^Whether the server made the session read-only is not known/);
    expect(standIn.deletes().map((d) => d.path)).toEqual([`${sessions}/s_1`]);
    await expect(tab.run(here, (c) => c.execute("SELECT 2"))).rejects.toBe(err);
    expect(standIn.created()).toHaveLength(1);
  });
});
