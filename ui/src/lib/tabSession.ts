// One editor tab's Warp SQL session. It opens on the tab's first run, with the tab's catalog
// and schema, and lasts across runs (temporary tables, USE and SET carry over), until the tab
// closes, the user asks for a new one, or the server ends it.
//
// The connection is opened with `reconnect: false`: when the server no longer has the session
// (it was idle too long, or the server restarted), this class decides what to do, because only
// it knows whether a transaction was open. With one open, nothing runs: the work done in it is
// gone, and running the rest on a new session would commit half a transaction. Without one,
// a new session takes over and the run goes on, with a note that temporary state is gone.

import { type Connection, ErrorCode, type TransactionState, WarpSqlError } from "@gizmodata/warp-sql";

import { setRoleSql } from "./roles";
import type { StatementStream } from "./script";

export interface SessionInfo {
  /** The Warp SQL session id. */
  id: string;
  /** The server's own id for the session (its logs, audit records and KILL SESSION use it). */
  serverId: string | undefined;
  transaction: TransactionState;
  catalog: string;
  schema: string;
  idleTimeoutS: number | undefined;
  /** The role it acts with ("": all of the user's roles), as this tab set it. */
  role: string;
}

export interface Where {
  catalog: string;
  schema: string;
  /** The role the session acts with (SET ROLE); "" or absent for all of the user's roles. */
  role?: string;
}

export type Opener = (where: Where) => Promise<Connection>;
/** Asks a new session for the server's own id of it, before anything else runs in it. */
export type Identify = (conn: Connection) => Promise<string | undefined>;

/** The server no longer had the session while a transaction was open: nothing ran. */
export class TransactionLostError extends Error {
  override readonly name = "TransactionLostError";
  constructor(readonly reason: string) {
    super(
      `This tab's session on the server ended (${reason}) while a transaction was open, so the ` +
        "transaction was rolled back and nothing from this run executed. Run your statements again " +
        "(starting with BEGIN if you still want a transaction).",
    );
  }
}

export function isSessionGone(err: unknown): boolean {
  return WarpSqlError.is(err) && err.code === ErrorCode.sessionNotFound;
}

const goneReason = "it was idle too long, or the server restarted";

export class TabSession {
  readonly #open: Opener;
  readonly #identify: Identify | undefined;
  #conn: Promise<Connection> | undefined;
  #serverId: string | undefined;
  #role = "";
  #info: SessionInfo | undefined;
  /** Why the last session was replaced (shown once as a note), if it was. */
  #replaced: string | undefined;

  constructor(open: Opener, identify?: Identify) {
    this.#open = open;
    this.#identify = identify;
  }

  get info(): SessionInfo | undefined {
    return this.#info;
  }

  get open(): boolean {
    return this.#conn !== undefined;
  }

  /** Takes the note about a replaced session, once. */
  takeReplacedNote(): string | undefined {
    const note = this.#replaced;
    this.#replaced = undefined;
    return note;
  }

  connection(where: Where): Promise<Connection> {
    if (!this.#conn) {
      const opening = this.#open(where).then(async (conn) => {
        this.#serverId = this.#identify ? await this.#identify(conn).catch(() => undefined) : undefined;
        this.#role = "";
        if (where.role) {
          await conn.execute(setRoleSql(where.role));
          this.#role = where.role;
        }
        return conn;
      });
      opening.catch(() => {
        if (this.#conn === opening) this.#conn = undefined;
      });
      this.#conn = opening;
    }
    return this.#conn;
  }

  /**
   * Runs `work` on the session. When the server no longer has it, a transaction that was open
   * is reported (TransactionLostError) and nothing runs; otherwise `work` runs again, once, on a
   * new session.
   */
  async run<T>(where: Where, work: (conn: Connection) => Promise<T>): Promise<T> {
    const conn = await this.connection(where);
    try {
      return await work(conn);
    } catch (err) {
      if (!isSessionGone(err)) throw err;
      this.#lost();
      if (this.#info && this.#info.transaction !== "none") {
        this.#info = undefined;
        throw new TransactionLostError(goneReason);
      }
      this.#info = undefined;
      this.#replaced =
        `A new session was started: the previous one ended (${goneReason}). ` +
        "Temporary tables and settings from it are gone.";
      return work(await this.connection(where));
    }
  }

  /** Reads the session document: its id, transaction, current catalog and schema. */
  async refresh(where: Where): Promise<SessionInfo | undefined> {
    if (!this.#conn) return this.#info;
    return this.run(where, (conn) => this.#read(conn));
  }

  /**
   * A statement on the session, as a stream. Its first answer (rows or a count) is awaited
   * inside run(), so a session the server lost is handled before any row is read.
   */
  stream(where: Where, sql: string, signal: AbortSignal): StatementStream {
    const opening = this.run(where, async (conn) => {
      const stream = conn.stream(sql, undefined, { signal });
      try {
        await stream.rowsAffected();
      } catch (err) {
        await stream.close().catch(() => {});
        throw err;
      }
      return stream;
    });
    opening.catch(() => {}); // the caller sees the failure through the methods below
    return {
      rowsAffected: async () => (await opening).rowsAffected(),
      schema: async () => (await opening).schema(),
      close: async () => (await opening).close(),
      async *[Symbol.asyncIterator]() {
        yield* await opening;
      },
    };
  }

  async #read(conn: Connection): Promise<SessionInfo> {
    const doc = await conn.getSession();
    const options = doc.options ?? {};
    this.#info = {
      id: doc.session_id,
      serverId: this.#serverId,
      role: this.#role,
      transaction: doc.transaction,
      catalog: typeof options["catalog"] === "string" ? options["catalog"] : "",
      schema: typeof options["schema"] === "string" ? options["schema"] : "",
      idleTimeoutS: doc.idle_timeout_s,
    };
    return this.#info;
  }

  /** Sets the session's catalog, schema and role now, if it is open (else at its opening). */
  async setWhere(where: Where): Promise<void> {
    if (!this.#conn) return;
    const options: Record<string, string> = {};
    if (where.catalog) options["catalog"] = where.catalog;
    if (where.schema) options["schema"] = where.schema;
    const role = where.role ?? "";
    await this.run(where, async (conn) => {
      if (Object.keys(options).length) await conn.setOptions(options);
      if (role !== this.#role) {
        await conn.execute(setRoleSql(role));
        this.#role = role;
      }
      await this.#read(conn);
    });
  }

  /** Ends the session (rolling back an open transaction); the next run opens a new one. */
  async close(): Promise<void> {
    const conn = this.#conn;
    this.#conn = undefined;
    this.#info = undefined;
    if (conn) await conn.then((c) => c.close()).catch(() => {});
  }

  #lost(): void {
    const conn = this.#conn;
    this.#conn = undefined;
    // The server has no session to end; this only releases the client's side.
    if (conn) void conn.then((c) => c.close()).catch(() => {});
  }
}
