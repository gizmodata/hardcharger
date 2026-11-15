import { type Connection, ChargewireError } from "@gizmodata/chargewire";
import { describe, expect, it } from "vitest";

import {
  CatalogRefusedError,
  ReadOnlyRefusedError,
  SettingsLostError,
  TabSession,
  TransactionLostError,
  type Where,
  isReadOnlyRefusal,
} from "../../src/lib/tabSession";

interface Fake {
  id: string;
  transaction: "none" | "open" | "aborted";
  gone: boolean;
  /** How the server reports the session gone: not found (the default), or ended for a reason. */
  endedReason?: string;
  closed: boolean;
  ran: string[];
  readOnly: boolean;
  ignoreReadOnly?: boolean;
  /** Temporary sequences the session holds (the server refuses read_only while it has any). */
  tempSequences: number;
  /** A statement ran inside the open transaction (the server refuses read_only then). */
  work: boolean;
  /** What each statement ran as: the session's read_only when it ran. */
  modes: boolean[];
  /** The next read_only request fails this way instead of being answered (an unknown outcome). */
  failReadOnly?: () => Error;
  /** How many times read_only was asked for. */
  switches: number;
}

/** What @gizmodata/chargewire 2.3.6 throws for an option the server refused (Session.#settled). */
function optionRefusal(code: string, detail: string): ChargewireError {
  return new ChargewireError({
    code,
    status: 400,
    detail,
    message: `session option "read_only": ${detail} (${code}, HTTP 400)`,
  });
}

const sequencesDetail = "read_only cannot be set while the session has temporary sequences: drop them first";
const transactionDetail =
  "read_only cannot be set while a transaction with work in it is open: commit or roll back first";

function opener() {
  const made: Fake[] = [];
  const open = async (where?: Where): Promise<Connection> => {
    const f: Fake = {
      id: `s_${made.length + 1}`,
      transaction: "none",
      gone: false,
      closed: false,
      ran: [],
      readOnly: where?.readOnly === true,
      tempSequences: 0,
      work: false,
      modes: [],
      switches: 0,
    };
    made.push(f);
    const gone = () =>
      f.endedReason
        ? new ChargewireError({ code: "session_ended", status: 401, reason: f.endedReason })
        : new ChargewireError({ code: "session_not_found", status: 404 });
    const conn = {
      sessionId: f.id,
      getSession: async () => {
        if (f.gone) throw gone();
        return {
          session_id: f.id,
          options: { catalog: "memory", schema: "main", read_only: f.readOnly },
          transaction: f.transaction,
        };
      },
      setOptions: async (options: Record<string, unknown>) => {
        if (options["read_only"] === true) f.switches++;
        const fail = options["read_only"] === true ? f.failReadOnly : undefined;
        f.failReadOnly = undefined;
        if (fail) throw fail();
        if (f.gone) throw gone();
        if (options["read_only"] === true && !f.readOnly && !f.ignoreReadOnly) {
          // As the server does (duckdb_session.cc SetReadOnly): a refusal leaves the session as it was.
          if (f.tempSequences > 0) throw optionRefusal("invalid_option_value", sequencesDetail);
          if (f.transaction !== "none" && f.work) throw optionRefusal("invalid_option_value", transactionDetail);
          f.readOnly = true;
        }
        return { read_only: f.readOnly };
      },
      execute: async (sql: string) => {
        if (f.gone) throw gone();
        f.ran.push(sql);
        f.modes.push(f.readOnly);
        if (sql === "BEGIN") {
          f.transaction = "open";
          f.work = false;
        } else if (sql === "COMMIT" || sql === "ROLLBACK") {
          f.transaction = "none";
        } else if (f.transaction !== "none") {
          f.work = true;
        }
        if (sql.startsWith("CREATE TEMP SEQUENCE")) f.tempSequences++;
        if (sql.startsWith("DROP SEQUENCE")) f.tempSequences--;
        return null;
      },
      close: async () => {
        f.closed = true;
      },
    };
    return conn as unknown as Connection;
  };
  return { made, open };
}

const here = { catalog: "memory", schema: "main" };

/** The server's refusal of its log catalog as a session's catalog (Hardcharger 2.5+). */
const refusal = () =>
  new ChargewireError({
    code: "permission_denied",
    status: 403,
    message:
      "catalog: the log catalog can't be the session's catalog (read access to catalog 'log_gizmosql_logs' is for administrators querying it by name)",
  });

describe("TabSession", () => {
  it("requests read-only before work and confirms it", async () => {
    const { made, open } = opener();
    const s = new TabSession(open);
    await s.run({ ...here, readOnly: true }, (c) => c.execute("SELECT 1"));
    expect(made[0]!.readOnly).toBe(true);
    expect((await s.refresh(here))?.readOnly).toBe(true);
  });

  it("closes an unconfirmed initial read-only connection before any work runs", async () => {
    const { made, open } = opener();
    const s = new TabSession(() => open()); // a server that ignores the creation option
    await expect(s.run({ ...here, readOnly: true }, (c) => c.execute("SELECT 1"))).rejects.toThrow("did not confirm");
    expect(made[0]!.closed).toBe(true);
    expect(made[0]!.ran).toEqual([]);
  });

  it("keeps an enabled restriction on automatic replacement but allows a fresh choice after close", async () => {
    const { made, open } = opener();
    const s = new TabSession(open);
    await s.connection(here);
    expect((await s.enableReadOnly(here)).readOnly).toBe(true);
    made[0]!.gone = true;
    await s.run(here, (c) => c.execute("SELECT 1"));
    expect(made[1]!.readOnly).toBe(true);
    await s.close();
    await s.run(here, (c) => c.execute("SELECT 2"));
    expect(made[2]!.readOnly).toBe(false);
  });

  it("preserves a server-imposed restriction when replacing the session", async () => {
    const { made, open } = opener();
    const s = new TabSession(async (where) => {
      const conn = await open(where);
      if (made.length === 1) made[0]!.readOnly = true;
      return conn;
    });
    await s.connection(here);
    made[0]!.gone = true;
    await s.run(here, (c) => c.execute("SELECT 1"));
    expect(made[1]!.readOnly).toBe(true);
  });

  it("blocks an unconfirmed transition until an explicit new session", async () => {
    const { made, open } = opener();
    const s = new TabSession(open);
    await s.connection(here);
    made[0]!.ignoreReadOnly = true;
    await expect(s.enableReadOnly(here)).rejects.toThrow("did not confirm");
    expect(made[0]!.closed).toBe(true);
    await expect(s.run(here, (c) => c.execute("CREATE TABLE t(i INT)"))).rejects.toThrow("New session");
    expect(made[0]!.ran).toEqual([]);
    expect(made).toHaveLength(1);
    await s.close();
    await s.run(here, (c) => c.execute("SELECT 1"));
    expect(made).toHaveLength(2);
  });

  it("opens on first use and keeps one session across runs", async () => {
    const { made, open } = opener();
    const s = new TabSession(open);
    expect(s.open).toBe(false);
    await s.run(here, (c) => c.execute("SELECT 1"));
    await s.run(here, (c) => c.execute("SELECT 2"));
    expect(made).toHaveLength(1);
    expect(made[0]!.ran).toEqual(["SELECT 1", "SELECT 2"]);
    expect((await s.refresh(here))?.id).toBe("s_1");
  });

  it("a session the server lost without a transaction is replaced, and the work runs once on the new one", async () => {
    const { made, open } = opener();
    const s = new TabSession(open);
    await s.run(here, (c) => c.execute("SELECT 1"));
    await s.refresh(here);
    made[0]!.gone = true;
    await s.run(here, (c) => c.execute("SELECT 2"));
    expect(made).toHaveLength(2);
    expect(made[1]!.ran).toEqual(["SELECT 2"]);
    expect(s.takeReplacedNote()).toMatch(/A new session was started.*Temporary tables and settings from it are gone/);
    expect(s.takeReplacedNote()).toBeUndefined();
  });

  it("a catalog the server keeps for querying by name: the session opens in its own, with a note", async () => {
    const { made, open } = opener();
    const asked: string[] = [];
    const s = new TabSession(async (where) => {
      asked.push(where.catalog);
      if (where.catalog === "log_gizmosql_logs") throw refusal();
      return open();
    });
    const logs = { catalog: "log_gizmosql_logs", schema: "main" };
    await s.run(logs, (c) => c.execute("SELECT * FROM log_gizmosql_logs.main.logs LIMIT 100"));
    expect(asked).toEqual(["log_gizmosql_logs", ""]); // refused, then its own catalog
    expect(made[0]!.ran).toEqual(["SELECT * FROM log_gizmosql_logs.main.logs LIMIT 100"]);
    expect(s.takeCatalogNote()).toMatch(/^log_gizmosql_logs can't be a tab's catalog on this server/);
    expect(s.takeCatalogNote()).toBeUndefined();
    // Any other refusal is the error it was.
    const t = new TabSession(async () => {
      throw new ChargewireError({ code: "permission_denied", status: 403, message: "no access to catalog x" });
    });
    await expect(t.run({ catalog: "x", schema: "main" }, (c) => c.execute("SELECT 1"))).rejects.toThrow(
      "no access to catalog x",
    );
  });

  it("switching an open session to such a catalog is refused with a note, and the session stays put", async () => {
    const { made, open } = opener();
    const s = new TabSession(async () => {
      const conn = await open();
      return Object.assign(conn, {
        setOptions: async (options: Record<string, string>) => {
          if (options["catalog"] === "log_gizmosql_logs") throw refusal();
          return options;
        },
      });
    });
    await s.run(here, (c) => c.execute("SELECT 1"));
    await s.refresh(here);
    const err = await s.setWhere({ catalog: "log_gizmosql_logs", schema: "main" }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(CatalogRefusedError);
    expect((err as Error).message).toMatch(/is for querying by name \(log_gizmosql_logs\.schema\.table\)/);
    expect(s.info?.catalog).toBe("memory");
    expect(made).toHaveLength(1);
  });

  it("a stale 'none' right after BEGIN doesn't let a lost session's run go again", async () => {
    // Servers before Chargewire 2.2.1 could answer BEGIN before recording it: the read right
    // after says "none". The tab still treats the transaction as open.
    const { made, open } = opener();
    const s = new TabSession(open);
    await s.run(here, (c) => c.execute("SELECT 1"));
    await s.run(here, (c) => c.execute("BEGIN"));
    s.noteStatement("BEGIN");
    made[0]!.transaction = "none"; // the stale read
    expect((await s.refresh(here))?.transaction).toBe("none");
    made[0]!.gone = true;
    await expect(s.run(here, (c) => c.execute("INSERT 1"))).rejects.toBeInstanceOf(TransactionLostError);
    expect(made).toHaveLength(1); // nothing ran on a new session
    // The next run starts afresh, in no transaction.
    await s.run(here, (c) => c.execute("SELECT 2"));
    expect(made).toHaveLength(2);
  });

  it("after USE or SET, a lost session's run doesn't go again on a new one", async () => {
    for (const setting of ["USE other", "SET search_path = 'other'", "PRAGMA threads = 2"]) {
      const { made, open } = opener();
      const s = new TabSession(open);
      await s.run(here, (c) => c.execute(setting));
      s.noteStatement(setting);
      made[0]!.gone = true;
      await expect(
        s.run(here, (c) => c.execute("INSERT 2")),
        setting,
      ).rejects.toBeInstanceOf(SettingsLostError);
      expect(made, setting).toHaveLength(1); // nothing ran on a new session
      // UPDATE ... SET is not a setting: a statement like it lets the run go again.
      await s.run(here, (c) => c.execute("UPDATE t SET v = 1"));
      s.noteStatement("UPDATE t SET v = 1");
      made[1]!.gone = true;
      await s.run(here, (c) => c.execute("INSERT 3"));
      expect(made, setting).toHaveLength(3);
    }
  });

  it("after COMMIT reads 'none', a lost session's run goes again on a new one", async () => {
    const { made, open } = opener();
    const s = new TabSession(open);
    await s.run(here, (c) => c.execute("BEGIN"));
    s.noteStatement("BEGIN");
    await s.run(here, (c) => c.execute("COMMIT"));
    s.noteStatement("COMMIT");
    expect((await s.refresh(here))?.transaction).toBe("none");
    made[0]!.gone = true;
    await s.run(here, (c) => c.execute("INSERT 2"));
    expect(made).toHaveLength(2);
    expect(made[1]!.ran).toEqual(["INSERT 2"]);
  });

  it("a session the server ended for an idle transaction says so, and a killed one is the error", async () => {
    const { made, open } = opener();
    const s = new TabSession(open);
    await s.run(here, (c) => c.execute("BEGIN"));
    expect((await s.refresh(here))?.transaction).toBe("open");
    made[0]!.gone = true;
    made[0]!.endedReason = "idle_in_transaction";
    await expect(s.run(here, (c) => c.execute("INSERT 1"))).rejects.toThrow(
      /the server ended it: it sat idle with a transaction open, past the server's timeout/,
    );
    expect(made).toHaveLength(1);
    // Without a transaction, a new session takes over, with the reason in the note.
    await s.run(here, (c) => c.execute("SELECT 1"));
    made[1]!.gone = true;
    made[1]!.endedReason = "idle_in_transaction";
    await s.run(here, (c) => c.execute("SELECT 2"));
    expect(made).toHaveLength(3);
    expect(s.takeReplacedNote()).toMatch(/it sat idle with a transaction open, past the server.s timeout/);
    // An administrator's kill is not something a new session cures: it is the error.
    made[2]!.gone = true;
    made[2]!.endedReason = "killed";
    await expect(s.run(here, (c) => c.execute("SELECT 3"))).rejects.toMatchObject({
      code: "session_ended",
      reason: "killed",
    });
    expect(made).toHaveLength(3);
  });

  it("a session lost with a transaction open runs nothing and says the transaction was rolled back", async () => {
    const { made, open } = opener();
    const s = new TabSession(open);
    await s.run(here, (c) => c.execute("BEGIN"));
    expect((await s.refresh(here))?.transaction).toBe("open");
    made[0]!.gone = true;
    await expect(s.run(here, (c) => c.execute("INSERT 1"))).rejects.toBeInstanceOf(TransactionLostError);
    expect(made).toHaveLength(1); // no new session was opened for it
    // The next run starts afresh.
    await s.run(here, (c) => c.execute("SELECT 3"));
    expect(made).toHaveLength(2);
    expect(made[1]!.ran).toEqual(["SELECT 3"]);
  });

  it("close ends the session; the next run opens a new one", async () => {
    const { made, open } = opener();
    const s = new TabSession(open);
    await s.run(here, (c) => c.execute("SELECT 1"));
    await s.close();
    expect(made[0]!.closed).toBe(true);
    expect(s.info).toBeUndefined();
    await s.run(here, (c) => c.execute("SELECT 2"));
    expect(made).toHaveLength(2);
  });

  it("other errors pass through unchanged", async () => {
    const { open } = opener();
    const s = new TabSession(open);
    const err = new ChargewireError({ code: "sql_error", status: 400 });
    await expect(s.run(here, () => Promise.reject(err))).rejects.toBe(err);
  });
});

describe("TabSession: the server's answer to Read-only", () => {
  it("a refusal with the session intact (temporary sequences) leaves it open and read-write; following the advice works", async () => {
    const { made, open } = opener();
    const s = new TabSession(open);
    await s.run(here, (c) => c.execute("CREATE TEMP TABLE scratch AS SELECT 42 AS v"));
    await s.run(here, (c) => c.execute("CREATE TEMP SEQUENCE seq"));
    await s.refresh(here);

    const err = await s.enableReadOnly(here).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ReadOnlyRefusedError);
    expect((err as ReadOnlyRefusedError).code).toBe("invalid_option_value");
    expect((err as ReadOnlyRefusedError).detail).toBe(sequencesDetail);
    expect((err as Error).message).toBe(
      `Read-only was not enabled: ${sequencesDetail}. The session is unchanged and still read-write; ` +
        "select Read-only again when that is done.",
    );
    expect((err as Error).message).not.toMatch(/closed|New session/);
    // The session is exactly as it was: open, the same one, read-write, nothing ended.
    expect(made[0]!.closed).toBe(false);
    expect(made[0]!.readOnly).toBe(false);
    expect(s.open).toBe(true);
    expect(s.readOnly).toBe(false);
    expect(s.info).toMatchObject({ id: "s_1", readOnly: false });

    // The server's advice can be followed in the same session, and the next tick locks it.
    await s.run(here, (c) => c.execute("DROP SEQUENCE seq"));
    expect(made).toHaveLength(1);
    expect(made[0]!.ran).toEqual([
      "CREATE TEMP TABLE scratch AS SELECT 42 AS v",
      "CREATE TEMP SEQUENCE seq",
      "DROP SEQUENCE seq",
    ]);
    expect(await s.enableReadOnly(here)).toMatchObject({ id: "s_1", readOnly: true });
    expect(made[0]!.readOnly).toBe(true);
    expect(s.readOnly).toBe(true);
    await s.run(here, (c) => c.execute("SELECT v FROM scratch"));
    expect(made).toHaveLength(1); // the temporary table's session, still
  });

  it("a refusal for an open transaction with work in it leaves the transaction open; after COMMIT the switch goes through", async () => {
    const { made, open } = opener();
    const s = new TabSession(open);
    await s.run(here, (c) => c.execute("BEGIN"));
    s.noteStatement("BEGIN");
    await s.run(here, (c) => c.execute("INSERT INTO t VALUES (1)"));
    // A tick sent while the tab does not know the transaction (its last read failed).
    const err = await s.enableReadOnly(here).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ReadOnlyRefusedError);
    expect((err as ReadOnlyRefusedError).detail).toBe(transactionDetail);
    expect(made[0]!.closed).toBe(false);
    expect(made[0]!.transaction).toBe("open"); // not rolled back
    expect(s.info).toMatchObject({ id: "s_1", transaction: "open", readOnly: false });
    await s.run(here, (c) => c.execute("COMMIT"));
    s.noteStatement("COMMIT");
    expect(made[0]!.ran).toEqual(["BEGIN", "INSERT INTO t VALUES (1)", "COMMIT"]);
    expect(await s.enableReadOnly(here)).toMatchObject({ id: "s_1", transaction: "none", readOnly: true });
    expect(made).toHaveLength(1);
  });

  it.each([
    ["invalid_option_value", 400],
    ["unknown_option", 400], // a server without read-only sessions
    ["read_only", 400],
    ["bad_request", 400],
    ["permission_denied", 403],
    ["rate_limited", 429],
    ["invalid_token", 401],
  ])("the server's refusal %s (HTTP %i) keeps the session", async (code, status) => {
    const { made, open } = opener();
    const s = new TabSession(open);
    await s.run(here, (c) => c.execute("SELECT 1"));
    made[0]!.failReadOnly = () => new ChargewireError({ code, status, detail: `refused: ${code}` });
    const err = await s.enableReadOnly(here).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ReadOnlyRefusedError);
    expect((err as Error).message).toContain(`refused: ${code}`);
    expect(made[0]!.closed).toBe(false);
    expect(s.open).toBe(true);
    await s.run(here, (c) => c.execute("SELECT 2"));
    expect(made).toHaveLength(1);
    expect(made[0]!.ran).toEqual(["SELECT 1", "SELECT 2"]);
  });

  it.each([
    ["no answer", () => new ChargewireError({ code: "client_transport_error", detail: "connection reset" })],
    ["a proxy's 502", () => new ChargewireError({ code: "client_http_error", status: 502, detail: "Bad Gateway" })],
    ["a server failure", () => new ChargewireError({ code: "internal_error", status: 500, detail: "boom" })],
    ["a refusal code with a 5xx", () => new ChargewireError({ code: "invalid_option_value", status: 503 })],
    ["an answer off the protocol", () => new ChargewireError({ code: "client_protocol_error", detail: "not JSON" })],
    ["an aborted request", () => new DOMException("The operation was aborted.", "AbortError")],
  ])("an unknown outcome (%s) stops using the session, says why, and blocks until New session", async (_, fail) => {
    const { made, open } = opener();
    const s = new TabSession(open);
    await s.run(here, (c) => c.execute("CREATE TEMP TABLE scratch AS SELECT 1"));
    made[0]!.failReadOnly = fail;
    const err = await s.enableReadOnly(here).catch((e: unknown) => e);
    expect(err).not.toBeInstanceOf(ReadOnlyRefusedError);
    expect((err as Error).message).toMatch(
      /^Whether the server made the session read-only is not known \(.+\)\. To be safe this tab no longer uses it: the session was closed, with its temporary tables, settings and any open transaction\. Choose New session to continue\.$/,
    );
    expect(made[0]!.closed).toBe(true);
    await expect(s.run(here, (c) => c.execute("CREATE TABLE t(i INT)"))).rejects.toBe(err);
    expect(made).toHaveLength(1);
    await s.close();
    await s.run(here, (c) => c.execute("SELECT 1"));
    expect(made).toHaveLength(2);
  });

  it("a session the server no longer has: the tab says it ended, and blocks until New session", async () => {
    const { made, open } = opener();
    const s = new TabSession(open);
    await s.run(here, (c) => c.execute("SELECT 1"));
    made[0]!.gone = true;
    const err = await s.enableReadOnly(here).catch((e: unknown) => e);
    expect((err as Error).message).toMatch(
      /^The session ended on the server before read-only could be confirmed \(.*session_not_found.*\)\. Its temporary tables, settings and any open transaction are gone\. Choose New session to continue\.$/,
    );
    expect(made[0]!.closed).toBe(true);
    await expect(s.run(here, (c) => c.execute("SELECT 2"))).rejects.toBe(err);
    expect(made).toHaveLength(1);
  });

  it("classifies by the server's code and status, never by the message", () => {
    const refusal = (code: string, status?: number, message?: string) =>
      isReadOnlyRefusal(new ChargewireError({ code, ...(status ? { status } : {}), ...(message ? { message } : {}) }));
    expect(refusal("invalid_option_value", 400)).toBe(true);
    expect(refusal("permission_denied", 403)).toBe(true);
    expect(refusal("invalid_option_value")).toBe(false); // no answer carried it
    expect(refusal("invalid_option_value", 500)).toBe(false);
    expect(refusal("session_not_found", 404)).toBe(false);
    expect(refusal("session_ended", 401)).toBe(false);
    expect(refusal("client_http_error", 400)).toBe(false); // a proxy's page, not the server's word
    expect(refusal("client_transport_error", undefined, "invalid_option_value: drop them first")).toBe(false);
    expect(isReadOnlyRefusal(new Error("invalid_option_value"))).toBe(false);
  });
});

describe("TabSession: a run that asks for read-only never uses a writable session", () => {
  it("an open read-write session is switched before the work runs, and the work runs read-only on it", async () => {
    const { made, open } = opener();
    const s = new TabSession(open);
    await s.run(here, (c) => c.execute("SELECT 1"));
    expect(made[0]!.readOnly).toBe(false);
    await s.run({ ...here, readOnly: true }, (c) => c.execute("CREATE TABLE t (i INT)"));
    expect(made).toHaveLength(1);
    expect(made[0]!.switches).toBe(1);
    expect(made[0]!.modes).toEqual([false, true]); // the second statement ran in a read-only session
    expect(s.readOnly).toBe(true);
    expect((await s.refresh(here))?.readOnly).toBe(true);
  });

  it("a refused switch fails the run with the server's words, runs nothing, and keeps the session", async () => {
    const { made, open } = opener();
    const s = new TabSession(open);
    await s.run(here, (c) => c.execute("CREATE TEMP SEQUENCE seq"));
    const err = await s
      .run({ ...here, readOnly: true }, (c) => c.execute("CREATE TABLE t (i INT)"))
      .catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ReadOnlyRefusedError);
    expect(made[0]!.ran).toEqual(["CREATE TEMP SEQUENCE seq"]); // the write never ran
    expect(made[0]!.closed).toBe(false);
    expect(s.readOnly).toBe(false);
    // Without the request, the session is used as it is (the box shows it read-write).
    await s.run(here, (c) => c.execute("DROP SEQUENCE seq"));
    expect(made).toHaveLength(1);
  });

  it("a session still opening read-only is not switched again", async () => {
    const { made, open } = opener();
    const s = new TabSession(open);
    const first = s.run({ ...here, readOnly: true }, (c) => c.execute("SELECT 1"));
    const second = s.run({ ...here, readOnly: true }, (c) => c.execute("SELECT 2"));
    await Promise.all([first, second]);
    expect(made).toHaveLength(1);
    expect(made[0]!.switches).toBe(0); // asked for at creation, confirmed then
    expect(made[0]!.modes).toEqual([true, true]);
  });

  it("reading or moving the session never changes its mode", async () => {
    const { made, open } = opener();
    const s = new TabSession(open);
    await s.run(here, (c) => c.execute("SELECT 1"));
    await s.refresh({ ...here, readOnly: true });
    await s.setWhere({ catalog: "memory", schema: "other", readOnly: true });
    expect(made[0]!.switches).toBe(0);
    expect(made[0]!.readOnly).toBe(false);
  });

  it("a replacement for a lost read-write session follows the session, not a later request", async () => {
    const { made, open } = opener();
    const s = new TabSession(open);
    await s.run(here, (c) => c.execute("SELECT 1"));
    made[0]!.gone = true;
    // A read after the loss replaces it, keeping its mode.
    await s.refresh({ ...here, readOnly: true });
    expect(made).toHaveLength(2);
    expect(made[1]!.readOnly).toBe(false);
    // A run that asks for read-only switches the replacement first.
    await s.run({ ...here, readOnly: true }, (c) => c.execute("SELECT 2"));
    expect(made[1]!.modes).toEqual([true]);
  });

  it("two tabs on one server: each session is confirmed on its own", async () => {
    const { made, open } = opener();
    const writable = new TabSession(open);
    const readOnly = new TabSession(open);
    await writable.run(here, (c) => c.execute("SELECT 1"));
    await readOnly.run({ ...here, readOnly: true }, (c) => c.execute("SELECT 2"));
    expect(made.map((f) => f.readOnly)).toEqual([false, true]);
    expect((await writable.refresh(here))?.readOnly).toBe(false);
    expect((await readOnly.refresh(here))?.readOnly).toBe(true);
  });
});

describe("TabSession: sessions opened later follow the tab's choice", () => {
  it("a replacement for a lost read-write session opens as the tab now asks (the connection's default changed)", async () => {
    const { made, open } = opener();
    const s = new TabSession(open);
    await s.run(here, (c) => c.execute("SELECT 1"));
    made[0]!.gone = true;
    await s.run({ ...here, openReadOnly: true }, (c) => c.execute("CREATE TABLE t (i INT)"));
    expect(made).toHaveLength(2);
    expect(made[1]!.readOnly).toBe(true);
    expect(made[1]!.modes).toEqual([true]);
    expect((await s.refresh(here))?.readOnly).toBe(true);
    // Twin: an open read-write session is left as it is by the same request.
    const t = new TabSession(open);
    await t.run(here, (c) => c.execute("SELECT 1"));
    await t.run({ ...here, openReadOnly: true }, (c) => c.execute("SELECT 2"));
    expect(made[2]!.modes).toEqual([false, false]);
    expect(made[2]!.switches).toBe(0);
  });

  it("a session the relay can no longer reach (signed in again) is replaced, with the reason; with a transaction open nothing runs", async () => {
    const relayGone = () =>
      new ChargewireError({ code: "remote_not_found", status: 404, detail: "no such remote server: r-1" });
    const { made, open } = opener();
    const s = new TabSession(async (where) => {
      const conn = await open(where);
      const f = made.at(-1)!;
      const execute = conn.execute.bind(conn);
      return Object.assign(conn, {
        execute: async (sql: string) => {
          if (f.endedReason === "relay") throw relayGone();
          return execute(sql);
        },
      });
    });
    await s.run(here, (c) => c.execute("SELECT 1"));
    made[0]!.endedReason = "relay";
    await s.run({ ...here, openReadOnly: true }, (c) => c.execute("SELECT 2"));
    expect(made).toHaveLength(2);
    expect(made[1]!.readOnly).toBe(true);
    expect(s.takeReplacedNote()).toMatch(/this page signed in to the server again/);
    // With a transaction open, nothing runs on a new session.
    await s.run(here, (c) => c.execute("BEGIN"));
    s.noteStatement("BEGIN");
    made[1]!.endedReason = "relay";
    await expect(s.run(here, (c) => c.execute("INSERT 1"))).rejects.toBeInstanceOf(TransactionLostError);
    expect(made).toHaveLength(2);
    // Another 404 code is not a lost session.
    const t = new TabSession(async () => {
      throw new ChargewireError({ code: "not_found", status: 404 });
    });
    await expect(t.run(here, (c) => c.execute("SELECT 1"))).rejects.toMatchObject({ code: "not_found" });
  });
});
