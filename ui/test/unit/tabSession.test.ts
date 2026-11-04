import { type Connection, ChargewireError } from "@gizmodata/chargewire";
import { describe, expect, it } from "vitest";

import { TabSession, TransactionLostError } from "../../src/lib/tabSession";

interface Fake {
  id: string;
  transaction: "none" | "open" | "aborted";
  gone: boolean;
  /** How the server reports the session gone: not found (the default), or ended for a reason. */
  endedReason?: string;
  closed: boolean;
  ran: string[];
}

function opener() {
  const made: Fake[] = [];
  const open = async (): Promise<Connection> => {
    const f: Fake = { id: `s_${made.length + 1}`, transaction: "none", gone: false, closed: false, ran: [] };
    made.push(f);
    const gone = () =>
      f.endedReason
        ? new ChargewireError({ code: "session_ended", status: 401, reason: f.endedReason })
        : new ChargewireError({ code: "session_not_found", status: 404 });
    const conn = {
      sessionId: f.id,
      getSession: async () => {
        if (f.gone) throw gone();
        return { session_id: f.id, options: { catalog: "memory", schema: "main" }, transaction: f.transaction };
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

describe("TabSession", () => {
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

  it("a session the server ended for an idle transaction says so, and a killed one is the error", async () => {
    const { made, open } = opener();
    const s = new TabSession(open);
    await s.run(here, (c) => c.execute("BEGIN"));
    expect((await s.refresh(here))?.transaction).toBe("open");
    made[0]!.gone = true;
    made[0]!.endedReason = "idle_in_transaction";
    await expect(s.run(here, (c) => c.execute("INSERT 1"))).rejects.toThrow(
      /idle past the server's idle_in_transaction_session_timeout/,
    );
    expect(made).toHaveLength(1);
    // Without a transaction, a new session takes over, with the reason in the note.
    await s.run(here, (c) => c.execute("SELECT 1"));
    made[1]!.gone = true;
    made[1]!.endedReason = "idle_in_transaction";
    await s.run(here, (c) => c.execute("SELECT 2"));
    expect(made).toHaveLength(3);
    expect(s.takeReplacedNote()).toMatch(/idle_in_transaction_session_timeout/);
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
