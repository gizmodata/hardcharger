// A tab's catalog and schema, through the real client (the vendored @gizmodata/chargewire).
// A tab opens its connection with its catalog and schema (EditorTab:
// openConnection({ catalog, schema, reconnect: false })) and, when it is pointed somewhere
// else, sets them on the open connection (TabSession.setWhere): catalog and schema together,
// or the catalog alone when the tab has no schema ("Use this catalog" on a catalog without
// schemas). Setting the catalog alone moves a session to that catalog's default schema, as SQL
// USE does.
//
// The client before 2.3.6 kept the schema it was given earlier and replayed it with the new
// catalog, into a session it opened to replace a lost one, or, on a server that was stopped
// when it connected, into the session its first request creates: the session then sat in
// <new catalog>.<old schema>, or could not be opened. The UI's tabs never met that, because of
// two things these tests hold in place:
//   - setWhere reads the session right after setting options, which creates a lazily
//     connected session at once, so option changes never pile up before the first request;
//   - a tab replaces a lost session itself (reconnect: false), opening the new one with the
//     tab's catalog and schema in one document.
// They pass on client 2.3.5 as well: they guard the UI's side, not the client's fix.
import { connect } from "@gizmodata/chargewire";
import { describe, expect, it } from "vitest";

import { TabSession } from "../../src/lib/tabSession";

interface Sent {
  method: string;
  path: string;
  body: unknown;
}

const sessions = "/chargewire/v1/sessions";

/** A stand-in for a server: stopped (discovery says so) or running, recording what it is sent. */
function server(state: "stopped" | "running") {
  const sent: Sent[] = [];
  let count = 0;
  let current = "";
  let options: Record<string, unknown> = {};
  const answer = (status: number, body: unknown, type = "application/json") =>
    new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "Content-Type": type } });
  const sessionDoc = () => ({ session_id: current, options, transaction: "none", idle_timeout_s: 900 });
  // Setting the catalog alone lands in its default schema, as on the server.
  const apply = (asked: Record<string, unknown>) => {
    if ("catalog" in asked && !("schema" in asked)) options = { ...options, catalog: asked["catalog"], schema: "main" };
    else options = { ...options, ...asked };
  };
  const optionsOf = (body: unknown) => (body as { options?: Record<string, unknown> }).options ?? {};
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
        capabilities: ["prepared_statements", "transactions", "long_poll"],
        auth: { grant_types: ["password"] },
        ...(state === "stopped" ? { state: "stopped" } : {}),
      });
    }
    if (request.method === "POST" && path === sessions) {
      current = `s_${++count}`;
      options = { catalog: "my", schema: "main" };
      apply(optionsOf(body));
      return answer(201, sessionDoc());
    }
    if (path.startsWith(`${sessions}/`) && !path.startsWith(`${sessions}/${current}`)) {
      // A session the server no longer has.
      return answer(
        404,
        {
          type: "urn:chargewire:error:session_not_found",
          title: "No such session",
          status: 404,
          code: "session_not_found",
        },
        "application/problem+json",
      );
    }
    if (path === `${sessions}/${current}`) {
      if (request.method === "GET") return answer(200, sessionDoc());
      if (request.method === "PATCH") {
        apply(optionsOf(body));
        return answer(200, { options, errors: {} });
      }
    }
    if (request.method === "POST" && path === `${sessions}/${current}/statements`) {
      return answer(200, { statement_id: "q_1", state: "succeeded", result: { kind: "update", rows_affected: 1 } });
    }
    return answer(204, null); // closes and deletes
  };
  return {
    sent,
    fetch,
    /** The server forgets the session (an idle eviction, a restart). */
    lose: () => {
      current = "(none)";
    },
    created: () => sent.filter((s) => s.method === "POST" && s.path === sessions),
    /** The session options each request asked for, creations and changes, in order. */
    asked: () =>
      sent
        .filter((s) => (s.method === "POST" && s.path === sessions) || s.method === "PATCH")
        .map((s) => optionsOf(s.body)),
    statements: () => sent.filter((s) => s.method === "POST" && s.path.endsWith("/statements")),
  };
}

/** A tab's session on `fetch`, opened the way EditorTab opens it. */
function tabOn(fetch: typeof globalThis.fetch): TabSession {
  return new TabSession((where) =>
    connect({
      url: "http://db.test:31337/chargewire",
      fetch,
      token: "t",
      ...(where.catalog ? { catalog: where.catalog } : {}),
      ...(where.schema ? { schema: where.schema } : {}),
      reconnect: false,
      resultPartitions: 1,
      logger: { warn: () => {} },
    }),
  );
}

const create = (conn: { execute: (sql: string) => Promise<unknown> }) => conn.execute("CREATE TABLE t (v INTEGER)");

describe("a tab's session is where the tab says it is", () => {
  it("opened in my.raw on a stopped server, then pointed at lake alone: the session sits in lake's default schema", async () => {
    const standIn = server("stopped");
    const tab = tabOn(standIn.fetch);
    await tab.connection({ catalog: "my", schema: "raw" });
    expect(standIn.created()).toEqual([]); // lazily connected: nothing but discovery was sent

    await tab.setWhere({ catalog: "lake", schema: "" });
    // Reading the session after the change created it: the change did not wait for a statement.
    expect(standIn.created()).toHaveLength(1);
    expect(standIn.statements()).toEqual([]);
    await tab.run({ catalog: "lake", schema: "" }, create);

    expect(standIn.asked()).toEqual([{ catalog: "my", schema: "raw" }, { catalog: "lake" }]);
    expect(standIn.statements()).toHaveLength(1);
    expect(tab.info).toMatchObject({ catalog: "lake", schema: "main" });
  });

  it("a schema, then a catalog alone, on an open session; the server then loses it: the tab's new session opens in the catalog alone", async () => {
    const standIn = server("running");
    const tab = tabOn(standIn.fetch);
    await tab.connection({ catalog: "my", schema: "main" });
    await tab.setWhere({ catalog: "", schema: "raw" });
    await tab.setWhere({ catalog: "lake", schema: "" });
    expect(standIn.asked()).toEqual([{ catalog: "my", schema: "main" }, { schema: "raw" }, { catalog: "lake" }]);
    expect(tab.info).toMatchObject({ id: "s_1", catalog: "lake", schema: "main" });

    standIn.lose();
    await tab.run({ catalog: "lake", schema: "" }, create);

    // The forcing step: the first session was refused, and a second one ran the statement.
    expect(standIn.created()).toHaveLength(2);
    expect(standIn.statements().map((s) => s.path)).toEqual([
      `${sessions}/s_1/statements`,
      `${sessions}/s_2/statements`,
    ]);
    // The replacement was opened by the tab, with what the tab holds: no schema left over.
    expect(standIn.asked().at(-1)).toEqual({ catalog: "lake" });
    expect(tab.takeReplacedNote()).toMatch(/A new session was started/);
    await tab.setWhere({ catalog: "lake", schema: "" });
    expect(tab.info).toMatchObject({ id: "s_2", catalog: "lake", schema: "main" });
  });

  it("a catalog and a schema set together travel in one document", async () => {
    const standIn = server("running");
    const tab = tabOn(standIn.fetch);
    await tab.connection({ catalog: "my", schema: "raw" });
    await tab.setWhere({ catalog: "lake", schema: "staging" });
    expect(standIn.asked()).toEqual([
      { catalog: "my", schema: "raw" },
      { catalog: "lake", schema: "staging" },
    ]);
    expect(tab.info).toMatchObject({ catalog: "lake", schema: "staging" });
  });
});
