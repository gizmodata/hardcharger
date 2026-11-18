// System Admin in the UI: an editor tab knows whether its session acts as System Admin only
// from what the session answers, asks again whenever that may have changed (SET ROLE, RESET
// ROLE, a statement refused while it acted as one, a read-only switch, a role change, a refusal
// elsewhere), and never carries it to a new session; the page offers the admin pages while a
// tab's session says yes, and closes them when none does.
import { type Connection, ChargewireError } from "@gizmodata/chargewire";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useAdminAccess } from "../../src/lib/adminConnection";
import {
  SystemAdminTabs,
  elevateSql,
  isSystemAdminRefusal,
  mayChangeRole,
  reportingRefusals,
  systemAdminCheckSql,
} from "../../src/lib/systemAdmin";
import { TabSession, type Where } from "../../src/lib/tabSession";
import { type AppSession, SessionContext } from "../../src/session";

afterEach(cleanup);

/**
 * A server session as Hardcharger 2.14 runs one: SET ROLE SYSTEM ADMIN makes it ask for it; it
 * acts as System Admin while its user holds it and the store answers; one that asks and does
 * not runs nothing until RESET ROLE (or SET ROLE of a role), its check included.
 */
interface FakeServer {
  holds: boolean;
  storeUp: boolean;
  /** No HARDCHARGER_IS_SYSTEM_ADMIN() (a server before 2.14). */
  old?: boolean;
}

interface FakeSession {
  id: string;
  asks: boolean;
  readOnly: boolean;
  aborted: boolean;
  gone: boolean;
  ran: string[];
  checks: number;
}

const denied = (detail: string) =>
  new ChargewireError({ code: "permission_denied", status: 403, detail, message: `${detail} (permission_denied)` });

function fakeServer(server: FakeServer) {
  const sessions: FakeSession[] = [];
  const acting = (s: FakeSession) => s.asks && server.holds && server.storeUp && !s.readOnly;
  const refuseIfLost = (s: FakeSession) => {
    if (!s.asks || acting(s)) return;
    throw denied(
      server.storeUp
        ? "Hardcharger blocked this statement: user alice no longer holds SYSTEM ADMIN, which SET ROLE made active. Run RESET ROLE to go on as alice"
        : "Hardcharger blocked this statement: user alice acts as System Admin here (SET ROLE SYSTEM ADMIN), which is asked of the access control store before every statement, and the store did not answer (unavailable). Run RESET ROLE to go on as alice",
    );
  };
  const gone = () => new ChargewireError({ code: "session_not_found", status: 404 });
  const run = (s: FakeSession, sql: string) => {
    if (s.gone) throw gone();
    s.ran.push(sql);
    if (sql === elevateSql) {
      if (!server.holds)
        throw denied("Hardcharger blocked SET ROLE SYSTEM ADMIN: user alice does not hold SYSTEM ADMIN");
      if (s.readOnly) throw denied("Hardcharger blocked SET ROLE SYSTEM ADMIN: a read-only session never acts as one");
      s.asks = true;
      return;
    }
    if (sql === "RESET ROLE" || sql.startsWith("SET ROLE ")) {
      s.asks = false;
      return;
    }
    refuseIfLost(s);
    if (s.aborted && sql !== "ROLLBACK")
      throw new ChargewireError({ code: "sql_error", status: 400, detail: "aborted" });
    if (sql === "FAIL") {
      s.aborted = true;
      throw new ChargewireError({ code: "sql_error", status: 400, detail: "boom" });
    }
    if (sql === "ROLLBACK") s.aborted = false;
  };
  const open = async (): Promise<Connection> => {
    const s: FakeSession = {
      id: `s_${sessions.length + 1}`,
      asks: false,
      readOnly: false,
      aborted: false,
      gone: false,
      ran: [],
      checks: 0,
    };
    sessions.push(s);
    const conn = {
      sessionId: s.id,
      closed: false,
      getSession: async () => {
        if (s.gone) throw gone();
        return {
          session_id: s.id,
          options: { catalog: "memory", schema: "main", read_only: s.readOnly },
          transaction: s.aborted ? "aborted" : "none",
        };
      },
      setOptions: async (options: Record<string, unknown>) => {
        if (s.gone) throw gone();
        // A session acting as System Admin that turns read-only stops acting as one.
        if (options["read_only"] === true) s.readOnly = true;
        return { read_only: s.readOnly };
      },
      execute: async (sql: string) => {
        run(s, sql);
        return null;
      },
      query: async (sql: string) => {
        if (sql === systemAdminCheckSql) {
          s.checks++;
          if (s.gone) throw gone();
          if (server.old)
            throw new ChargewireError({
              code: "sql_error",
              status: 400,
              detail: "Scalar Function with name hardcharger_is_system_admin does not exist!",
            });
          refuseIfLost(s);
          if (s.aborted) throw new ChargewireError({ code: "sql_error", status: 400, detail: "aborted" });
          const row = { system_admin: acting(s) };
          return { get: () => ({ toJSON: () => row }) };
        }
        run(s, sql);
        return { get: () => undefined };
      },
      stream: (sql: string) => {
        let done: Promise<void> | undefined;
        const once = () => (done ??= Promise.resolve().then(() => run(s, sql)));
        return {
          statementId: undefined,
          rowsAffected: async () => {
            await once();
            return null;
          },
          schema: async () => undefined,
          close: async () => {},
          async *[Symbol.asyncIterator]() {},
        };
      },
      close: async () => {
        conn.closed = true;
      },
    };
    return conn as unknown as Connection;
  };
  // The test changes the server (a revoke, the store going away) through what it returns.
  return Object.assign(server, { sessions, open });
}

const here: Where = { catalog: "memory", schema: "main" };

/** Runs one statement the way a tab does (stream, then the tab's read of the session). */
async function runInTab(tab: TabSession, sql: string): Promise<boolean | undefined> {
  const stream = tab.stream(here, sql, new AbortController().signal);
  await stream.rowsAffected().catch(() => {});
  return (await tab.refresh(here))?.systemAdmin;
}

describe("a tab's session and System Admin", () => {
  it("is one only after SET ROLE SYSTEM ADMIN, and not after RESET ROLE", async () => {
    const server = fakeServer({ holds: true, storeUp: true });
    const tab = new TabSession(server.open);
    expect(await runInTab(tab, "SELECT 1")).toBe(false);
    expect(server.sessions[0]!.checks).toBe(0); // nothing that could change it: not asked
    expect(await runInTab(tab, elevateSql)).toBe(true);
    expect(tab.systemAdmin).toBe(true);
    expect(server.sessions[0]!.checks).toBe(1);
    // Statements that ran as System Admin say it still is: no question each run.
    expect(await runInTab(tab, "SELECT 2")).toBe(true);
    expect(server.sessions[0]!.checks).toBe(1);
    expect(await runInTab(tab, "RESET ROLE")).toBe(false);
    expect(server.sessions[0]!.checks).toBe(2);
  });

  it("asks after a SET ROLE that failed, and is not one when the user does not hold it", async () => {
    const server = fakeServer({ holds: false, storeUp: true });
    const tab = new TabSession(server.open);
    expect(await runInTab(tab, elevateSql)).toBe(false);
    expect(server.sessions[0]!.checks).toBe(1);
  });

  it("follows a revoke: refused, then asked at every read until RESET ROLE", async () => {
    const server = fakeServer({ holds: true, storeUp: true });
    const tab = new TabSession(server.open);
    expect(await runInTab(tab, elevateSql)).toBe(true);
    server.holds = false; // revoked, on this replica or another: the server refuses at once
    expect(await runInTab(tab, "SELECT 2")).toBe(false);
    const asked = server.sessions[0]!.checks;
    expect(await runInTab(tab, "SELECT 3")).toBe(false);
    expect(server.sessions[0]!.checks).toBe(asked + 1); // still asking: the server could undo it
    // Granted again before RESET ROLE: the server acts as System Admin again, and so does the tab.
    server.holds = true;
    expect(await runInTab(tab, "SELECT 4")).toBe(true);
    server.holds = false;
    expect(await runInTab(tab, "SELECT 5")).toBe(false);
    expect(await runInTab(tab, "RESET ROLE")).toBe(false);
    const after = server.sessions[0]!.checks;
    expect(await runInTab(tab, "SELECT 6")).toBe(false);
    expect(server.sessions[0]!.checks).toBe(after); // asked nothing more: it is the user again
  });

  it("is not one while the store does not answer, and is again when it does", async () => {
    const server = fakeServer({ holds: true, storeUp: true });
    const tab = new TabSession(server.open);
    expect(await runInTab(tab, elevateSql)).toBe(true);
    server.storeUp = false;
    expect(await runInTab(tab, "SELECT 1")).toBe(false);
    server.storeUp = true;
    expect(await runInTab(tab, "SELECT 2")).toBe(true);
  });

  it("never carries it to a new session, and never says SET ROLE SYSTEM ADMIN itself", async () => {
    const server = fakeServer({ holds: true, storeUp: true });
    const tab = new TabSession(server.open);
    expect(await runInTab(tab, elevateSql)).toBe(true);
    // Idle too long, or a restart. SET ROLE is a setting: the run that finds the session gone
    // runs nothing (SettingsLostError), and the tab is not one any more.
    server.sessions[0]!.gone = true;
    await expect(tab.stream(here, "SELECT 1", new AbortController().signal).rowsAffected()).rejects.toThrow(
      "nothing from this run executed",
    );
    expect(tab.systemAdmin).toBe(false);
    expect(tab.info).toBeUndefined();
    // The next run opens a new session, which is the user's, with nothing said for it.
    expect(await runInTab(tab, "SELECT 1")).toBe(false);
    expect(server.sessions).toHaveLength(2);
    expect(server.sessions[1]!.ran).toEqual(["SELECT 1"]);
    expect(server.sessions[1]!.asks).toBe(false);
    // Without a SET ROLE the tab knows of (a session replaced after a plain SELECT), the same.
    server.sessions[1]!.gone = true;
    expect(await runInTab(tab, "SELECT 2")).toBe(false);
    expect(server.sessions[2]!.ran).toEqual(["SELECT 2"]);
    // New session: the same.
    expect(await runInTab(tab, elevateSql)).toBe(true);
    await tab.close();
    expect(tab.systemAdmin).toBe(false);
    expect(await runInTab(tab, "SELECT 3")).toBe(false);
    expect(server.sessions[3]!.ran).toEqual(["SELECT 3"]);
  });

  it("asks again after the session turns read-only, which ends it", async () => {
    const server = fakeServer({ holds: true, storeUp: true });
    const tab = new TabSession(server.open);
    expect(await runInTab(tab, elevateSql)).toBe(true);
    const info = await tab.enableReadOnly(here);
    expect(info.readOnly).toBe(true);
    expect(info.systemAdmin).toBe(false);
  });

  it("asks again after the Run as picker changes the role", async () => {
    const server = fakeServer({ holds: true, storeUp: true });
    const tab = new TabSession(server.open);
    expect(await runInTab(tab, elevateSql)).toBe(true);
    await tab.setWhere({ ...here, role: "clerks" });
    expect(tab.info?.systemAdmin).toBe(false);
    expect(server.sessions[0]!.ran.at(-1)).toBe('SET ROLE "clerks"');
  });

  it("counts an unanswered question as no, and asks again while it was one", async () => {
    const server = fakeServer({ holds: true, storeUp: true });
    const tab = new TabSession(server.open);
    expect(await runInTab(tab, elevateSql)).toBe(true);
    expect(await runInTab(tab, "FAIL")).toBe(false); // an aborted transaction answers nothing
    expect(await runInTab(tab, "ROLLBACK")).toBe(true);
  });

  it("asks a server without System Admin once, not at every read", async () => {
    const server = fakeServer({ holds: false, storeUp: true, old: true });
    const tab = new TabSession(server.open);
    expect(await runInTab(tab, "SET threads = 2")).toBe(false);
    expect(await runInTab(tab, "SELECT 1")).toBe(false);
    expect(server.sessions[0]!.checks).toBe(1);
  });

  it("is no at once when asked to ask again, until the session says otherwise", async () => {
    const server = fakeServer({ holds: true, storeUp: true });
    const tab = new TabSession(server.open);
    expect(await runInTab(tab, elevateSql)).toBe(true);
    tab.askSystemAdminAgain();
    expect(tab.systemAdmin).toBe(false);
    expect(tab.info?.systemAdmin).toBe(false);
    expect((await tab.refresh(here))?.systemAdmin).toBe(true);
  });
});

describe("statements and refusals", () => {
  it("knows which statements may change what a session acts as", () => {
    for (const sql of [elevateSql, "reset role", "/* x */ SET ROLE dba", "EXECUTE p", "RESET ALL", "set threads=1"])
      expect(mayChangeRole(sql), sql).toBe(true);
    for (const sql of ["SELECT 'SET ROLE SYSTEM ADMIN'", "BEGIN", "-- SET ROLE\nSELECT 1"])
      expect(mayChangeRole(sql), sql).toBe(false);
  });

  it("tells a refusal as System Admin from other errors", () => {
    expect(isSystemAdminRefusal(denied("user alice no longer holds SYSTEM ADMIN, which SET ROLE made active"))).toBe(
      true,
    );
    expect(isSystemAdminRefusal(denied("user alice acts as System Admin here ..., and the store did not answer"))).toBe(
      true,
    );
    expect(isSystemAdminRefusal(denied("user alice has no SELECT privilege on table vault"))).toBe(false);
    expect(isSystemAdminRefusal(new ChargewireError({ code: "sql_error", status: 400, detail: "SYSTEM ADMIN" }))).toBe(
      false,
    );
    expect(isSystemAdminRefusal(new Error("does not hold SYSTEM ADMIN"))).toBe(false);
  });

  it("reports an admin page's refusals as System Admin, and passes every answer through", async () => {
    const onRefusal = vi.fn();
    const target = {
      sessionId: "s_1",
      ok: async (n: number) => n + 1,
      refused: async () => {
        throw denied("user alice no longer holds SYSTEM ADMIN");
      },
      other: async () => {
        throw denied("no SELECT privilege");
      },
    };
    const conn = reportingRefusals(target as unknown as Connection, onRefusal) as unknown as typeof target;
    expect(conn.sessionId).toBe("s_1");
    expect(await conn.ok(1)).toBe(2);
    await expect(conn.other()).rejects.toThrow("no SELECT privilege");
    expect(onRefusal).not.toHaveBeenCalled();
    await expect(conn.refused()).rejects.toThrow("no longer holds SYSTEM ADMIN");
    expect(onRefusal).toHaveBeenCalledTimes(1);
  });
});

describe("the page's record of tabs acting as System Admin", () => {
  it("is elevated while a tab's session says so, and tells its readers when that changes", () => {
    const tabs = new SystemAdminTabs();
    const heard = vi.fn();
    tabs.subscribe(heard);
    tabs.report("a", false, () => {});
    expect(tabs.elevated).toBe(false);
    expect(heard).not.toHaveBeenCalled();
    tabs.report("a", true, () => {});
    tabs.report("b", true, () => {});
    expect(tabs.elevated).toBe(true);
    expect(tabs.tabs).toEqual(["a", "b"]);
    expect(heard).toHaveBeenCalledTimes(1);
    tabs.forget("a");
    expect(tabs.elevated).toBe(true);
    tabs.report("b", false, () => {});
    expect(tabs.elevated).toBe(false);
    expect(heard).toHaveBeenCalledTimes(2);
  });

  it("on a refusal elsewhere, counts every tab as no until its session answers again", () => {
    const tabs = new SystemAdminTabs();
    const askA = vi.fn();
    const askB = vi.fn();
    tabs.report("a", true, askA);
    tabs.report("b", false, askB);
    tabs.recheck();
    expect(tabs.elevated).toBe(false);
    expect(askA).toHaveBeenCalledTimes(1);
    expect(askB).not.toHaveBeenCalled(); // it wasn't one: nothing to ask
    tabs.report("a", true, askA); // its session said yes again
    expect(tabs.elevated).toBe(true);
  });

  function Access() {
    const access = useAdminAccess();
    return <p data-testid="access">{`${access.allowed}/${access.systemAdmin}`}</p>;
  }

  function page(session: Partial<AppSession>) {
    const app = {
      isAdmin: false,
      localServer: true,
      identity: { user: "alice", role: "user", fullAdmin: false },
      ...session,
    } as AppSession;
    return render(
      <SessionContext value={app}>
        <Access />
      </SessionContext>,
    );
  }

  it("offers the admin pages to a user only while one of their tabs acts as System Admin", () => {
    const tabs = new SystemAdminTabs();
    page({ systemAdmin: tabs });
    expect(screen.getByTestId("access").textContent).toBe("false/false");
    act(() => tabs.report("a", true, () => {}));
    expect(screen.getByTestId("access").textContent).toBe("true/true");
    act(() => tabs.report("a", false, () => {}));
    expect(screen.getByTestId("access").textContent).toBe("false/false");
  });

  it("offers them to the administrator as before, never through System Admin", () => {
    const tabs = new SystemAdminTabs();
    tabs.report("a", true, () => {});
    page({
      isAdmin: true,
      identity: { user: "admin", role: "admin", fullAdmin: true } as AppSession["identity"],
      systemAdmin: tabs,
    });
    expect(screen.getByTestId("access").textContent).toBe("true/false");
  });

  it("offers nothing through System Admin without a local server", () => {
    const tabs = new SystemAdminTabs();
    tabs.report("a", true, () => {});
    page({ localServer: false, systemAdmin: tabs });
    expect(screen.getByTestId("access").textContent).toBe("false/false");
  });
});
