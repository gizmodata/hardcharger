import { type Connection, ChargewireError } from "@gizmodata/chargewire";
import { describe, expect, it } from "vitest";

import {
  CatalogRefusedError,
  SettingsLostError,
  TabSession,
  TransactionLostError,
  type Where,
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
}

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
        if (f.gone) throw gone();
        if (options["read_only"] === true && !f.ignoreReadOnly) f.readOnly = true;
        return { read_only: f.readOnly };
      },
      execute: async (sql: string) => {
        if (f.gone) throw gone();
        f.ran.push(sql);
        if (sql === "BEGIN") f.transaction = "open";
        if (sql === "COMMIT") f.transaction = "none";
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
