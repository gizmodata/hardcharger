// One editor tab's Chargewire session. It opens on the tab's first run, with the tab's catalog
// and schema, and lasts across runs (temporary tables, USE and SET carry over), until the tab
// closes, the user asks for a new one, or the server ends it.
//
// The connection is opened with `reconnect: false`: when the server no longer has the session
// (it was idle too long, or the server restarted), this class decides what to do, because only
// it knows whether a transaction was open. With one open, nothing runs: the work done in it is
// gone, and running the rest on a new session would commit half a transaction. Without one,
// a new session takes over and the run goes on, with a note that temporary state is gone.

import {
  type Connection,
  type StatementDocument,
  ErrorCode,
  type TransactionState,
  ChargewireError,
} from "@gizmodata/chargewire";

import { setRoleSql } from "./roles";
import { firstKeyword } from "./sql";
import { actsAsSystemAdmin, isSystemAdminRefusal, mayChangeRole } from "./systemAdmin";
import type { StatementStream } from "./script";

export interface SessionInfo {
  /** The Chargewire session id. */
  id: string;
  /** The server's own id for the session (its logs, audit records and KILL SESSION use it). */
  serverId: string | undefined;
  transaction: TransactionState;
  catalog: string;
  schema: string;
  idleTimeoutS: number | undefined;
  /** The role it acts with ("": all of the user's roles), as this tab set it. */
  role: string;
  readOnly: boolean;
  /**
   * The session acts as System Admin (SET ROLE SYSTEM ADMIN), as it last answered when asked
   * (HARDCHARGER_IS_SYSTEM_ADMIN()); see TabSession.systemAdmin.
   */
  systemAdmin: boolean;
}

export interface Where {
  catalog: string;
  schema: string;
  /** The role the session acts with (SET ROLE); "" or absent for all of the user's roles. */
  role?: string;
  /** The work must run in a read-only session: an open one that isn't is switched first. */
  readOnly?: boolean;
  /**
   * A session opened now (the first, or one replacing a lost one) opens read-only: the tab's
   * choice for new sessions. An open session is left as it is.
   */
  openReadOnly?: boolean;
}

export type Opener = (where: Where) => Promise<Connection>;
/** Asks a new session for the server's own id of it, before anything else runs in it. */
export type Identify = (conn: Connection) => Promise<string | undefined>;

/** The server no longer had the session after USE or SET ran in it: nothing ran. */
export class SettingsLostError extends Error {
  override readonly name = "SettingsLostError";
  constructor(readonly reason: string) {
    super(
      `This tab's session on the server ended (${reason}), and what USE or SET had set in it is gone ` +
        "with it, so nothing from this run executed: it could have run against another schema or with " +
        "other settings. Run USE or SET again, then your statements.",
    );
  }
}

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

/**
 * The server refused a catalog as the session's catalog: Hardcharger's own instrumentation and
 * log catalogs are for querying by name (catalog.schema.table), never a session's catalog.
 */
export function isCatalogRefused(err: unknown): boolean {
  if (!ChargewireError.is(err) || err.code !== ErrorCode.permissionDenied) return false;
  return /can't be the session's catalog|session option "catalog"/.test(err.message);
}

/** A catalog this tab can't make its own: what the tab did instead. */
export class CatalogRefusedError extends Error {
  override readonly name = "CatalogRefusedError";
  constructor(readonly catalog: string) {
    super(catalogNote(catalog));
  }
}

function catalogNote(catalog: string): string {
  return (
    `${catalog} can't be a tab's catalog on this server: it is for querying by name ` +
    `(${catalog}.schema.table). The tab stays in its own catalog.`
  );
}

/**
 * This page's relay no longer has the remote server the session was opened through: the page
 * registered the server again (signed in to it again), which replaces the earlier registration,
 * or the relay restarted. The session can't be reached any more.
 */
const remoteNotFound = "remote_not_found";

export function isSessionGone(err: unknown): boolean {
  return (
    ChargewireError.is(err) &&
    (err.code === ErrorCode.sessionNotFound ||
      (err.code === remoteNotFound && err.status === 404) ||
      (err.code === ErrorCode.sessionEnded && err.reason === "idle_in_transaction"))
  );
}

/** Why the session went, for the notes: the server's reason when it gave one. */
function goneReasonOf(err: unknown): string {
  if (ChargewireError.is(err) && err.code === remoteNotFound) {
    return "this page signed in to the server again, which leaves the sessions it had there unreachable";
  }
  if (ChargewireError.is(err) && err.code === ErrorCode.sessionEnded && err.reason === "idle_in_transaction") {
    // Its idle timeout, or its idle-in-transaction timeout, ended it while a transaction was open
    // (Chargewire §5.7: either way the reason is idle_in_transaction).
    return "the server ended it: it sat idle with a transaction open, past the server's timeout";
  }
  return "it was idle too long, or the server restarted";
}

/**
 * The server answered a request to make the session read-only with a refusal and did nothing
 * (spec §5.1, §5.3): an option it refused (`errors.read_only` in a 200, which the client throws
 * with the option's code and status 400), or the request as a whole (a 4xx problem document)
 * for a reason that leaves the session as it was. Judged by the server's code and status only;
 * a client-side code (`client_*`: no answer, or one that isn't the protocol's), a 5xx, or a
 * code about the session itself (not found, ended) is not a refusal: the outcome is unknown.
 */
const refusalCodes: readonly string[] = [
  ErrorCode.invalidOptionValue, // e.g. "temporary sequences: drop them first", "a transaction with work in it is open"
  ErrorCode.unknownOption, // a server without read-only sessions
  ErrorCode.readOnly,
  ErrorCode.badRequest,
  ErrorCode.permissionDenied,
  ErrorCode.rateLimited,
  ErrorCode.invalidToken,
];

export function isReadOnlyRefusal(err: unknown): err is ChargewireError {
  return (
    ChargewireError.is(err) &&
    refusalCodes.includes(err.code) &&
    err.status !== undefined &&
    err.status >= 400 &&
    err.status < 500
  );
}

/** The server refused to make the session read-only and left it exactly as it was. */
export class ReadOnlyRefusedError extends Error {
  override readonly name = "ReadOnlyRefusedError";
  /** The server's code (`invalid_option_value`, `unknown_option`, ...). */
  readonly code: string;
  /** The server's own words, for people. */
  readonly detail: string;
  constructor(cause: ChargewireError) {
    const detail = cause.detail || cause.title || cause.message;
    super(
      `Read-only was not enabled: ${detail.replace(/[.\s]+$/, "")}. ` +
        "The session is unchanged and still read-write; select Read-only again when that is done.",
      { cause },
    );
    this.code = cause.code;
    this.detail = detail;
  }
}

export class TabSession {
  readonly #open: Opener;
  readonly #identify: Identify | undefined;
  #conn: Promise<Connection> | undefined;
  #serverId: string | undefined;
  #role = "";
  #readOnly = false;
  #modeFailure: Error | undefined;
  #info: SessionInfo | undefined;
  /** A statement that may have opened a transaction ran, and none has been seen to end it. */
  #maybeOpen = false;
  /** A statement that may have ended the transaction ran since #maybeOpen was set. */
  #endedSinceOpen = false;
  /** USE, SET, RESET or PRAGMA ran in this session (a new one starts without what they set). */
  #settingsChanged = false;
  /** Why the session opened in another catalog than the tab asked for, until shown. */
  #catalogNote: string | undefined;
  /** Why the last session was replaced (shown once as a note), if it was. */
  #replaced: string | undefined;
  /** The open session said it acts as System Admin the last time it was asked. */
  #systemAdmin = false;
  /** What the session acts as may have changed since it was last asked: ask at the next read. */
  #askSystemAdmin = false;

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

  /**
   * The server confirmed read-only for this tab's session: the open one, or, with none open,
   * the last one (its replacement opens read-only too, until New session: close()).
   */
  get readOnly(): boolean {
    return this.#readOnly;
  }

  /**
   * The open session acts as System Admin, as it answered when last asked. It is asked again
   * at the next read (refresh) after anything that may change it: SET, RESET or EXECUTE, a
   * statement that failed while it acted as one (a revoke shows up as a refusal), a change of
   * role or read-only mode, or askSystemAdminAgain(). A new session (a replacement too) never
   * acts as one: the server never carries it over, and this tab never says SET ROLE SYSTEM
   * ADMIN on its own.
   */
  get systemAdmin(): boolean {
    return this.#systemAdmin;
  }

  /**
   * Something outside the tab was refused as System Admin: count the session as not acting as
   * one until it says otherwise, at the next read.
   */
  askSystemAdminAgain(): void {
    if (!this.#conn) return;
    this.#systemAdmin = false;
    this.#askSystemAdmin = true;
    if (this.#info) this.#info = { ...this.#info, systemAdmin: false };
  }

  /** Takes the note about a catalog the session couldn't open in, once. */
  takeCatalogNote(): string | undefined {
    const note = this.#catalogNote;
    this.#catalogNote = undefined;
    return note;
  }

  /** Takes the note about a replaced session, once. */
  takeReplacedNote(): string | undefined {
    const note = this.#replaced;
    this.#replaced = undefined;
    return note;
  }

  /**
   * The session, opened on first use with `where` (read-only when `where` asks for it, or when
   * this tab's last session was). An open session asked to be read-only that the server has not
   * confirmed so is never handed out as it is: it is switched first (the work waits for the
   * server's answer), and a refusal fails the work with nothing run (ReadOnlyRefusedError).
   */
  connection(where: Where): Promise<Connection> {
    if (this.#modeFailure) return Promise.reject(this.#modeFailure);
    if (this.#conn && where.readOnly === true && !this.#readOnly) {
      const current = this.#conn;
      return current.then(async (conn) => {
        // Opened read-only meanwhile (a session that was still opening), or switched by
        // another caller: nothing to do.
        if (this.#readOnly) return conn;
        if (this.#conn !== current) return this.connection(where);
        await this.#switchReadOnly(conn);
        return conn;
      });
    }
    if (!this.#conn) {
      where = { ...where, readOnly: this.#readOnly || where.readOnly === true || where.openReadOnly === true };
      // A catalog the server keeps for querying by name (its log or instrumentation catalog,
      // as a preview of one of its tables asks for): the session opens in its own catalog,
      // and the statement, which names the catalog, still runs.
      const opened = this.#open(where).catch((err: unknown) => {
        if (!where.catalog || !isCatalogRefused(err)) throw err;
        this.#catalogNote = catalogNote(where.catalog);
        return this.#open({ ...where, catalog: "", schema: "" });
      });
      const opening = opened.then(async (conn) => {
        try {
          // Ordinary connections to stopped servers stay lazy. A requested restriction
          // must be confirmed before returning even when it wakes the server.
          if (where.readOnly || conn.sessionId) {
            const doc = await conn.getSession();
            if (where.readOnly && doc.options?.["read_only"] !== true) {
              throw new Error("The server did not confirm read-only mode. Start a new session to continue.");
            }
            this.#readOnly ||= doc.options?.["read_only"] === true;
          }
        } catch (err) {
          await conn.close().catch(() => {});
          throw err;
        }
        this.#serverId = this.#identify ? await this.#identify(conn).catch(() => undefined) : undefined;
        this.#systemAdmin = false;
        this.#askSystemAdmin = false;
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
      const goneReason = goneReasonOf(err);
      const maybeOpen = this.#maybeOpen || (this.#info !== undefined && this.#info.transaction !== "none");
      const settingsLost = this.#settingsChanged;
      this.#lost();
      if (maybeOpen) {
        this.#info = undefined;
        throw new TransactionLostError(goneReason);
      }
      if (settingsLost) {
        this.#info = undefined;
        throw new SettingsLostError(goneReason);
      }
      this.#info = undefined;
      this.#replaced =
        `A new session was started: the previous one ended (${goneReason}). ` +
        "Temporary tables and settings from it are gone.";
      return work(await this.connection(where));
    }
  }

  /**
   * Notes a statement this tab ran, once the server answered it: one that may open a
   * transaction (BEGIN, START) makes the transaction possibly open, whatever a read right after
   * it says (servers before Chargewire 2.2.1 could answer a BEGIN before recording it, so the
   * session still said "none"); only a read that says "none" after one that ends it (COMMIT,
   * END, ROLLBACK, ABORT) clears that.
   */
  noteStatement(sql: string): void {
    const word = firstKeyword(sql);
    // Where and how statements run: a new session starts without it, so a run that finds the
    // session gone doesn't go again there (it could write to another schema's table).
    if (word === "USE" || word === "SET" || word === "RESET" || word === "PRAGMA") this.#settingsChanged = true;
    if (mayChangeRole(sql)) this.#askSystemAdmin = true;
    if (word === "BEGIN" || word === "START") {
      this.#maybeOpen = true;
      this.#endedSinceOpen = false;
    } else if (word === "COMMIT" || word === "END" || word === "ROLLBACK" || word === "ABORT") {
      this.#endedSinceOpen = true;
    }
  }

  /** Reads the session document: its id, transaction, current catalog and schema. */
  async refresh(where: Where): Promise<SessionInfo | undefined> {
    if (!this.#conn) return this.#info;
    // Reading the session never changes its mode (a replacement opens as openReadOnly asks, and
    // read-only when this one was).
    return this.run({ ...where, readOnly: false }, (conn) => this.#read(conn));
  }

  /**
   * A statement on the session, as a stream. Its first answer (rows or a count) is awaited
   * inside run(), so a session the server lost is handled before any row is read.
   */
  stream(
    where: Where,
    sql: string,
    signal: AbortSignal,
    onProgress?: (doc: StatementDocument) => void,
  ): StatementStream {
    let submitted: { readonly statementId: string | undefined } | undefined;
    const opening = this.run(where, async (conn) => {
      const stream = conn.stream(sql, undefined, {
        signal,
        onProgress: (doc) => {
          onProgress?.(doc);
        },
      });
      submitted = stream;
      try {
        await stream.rowsAffected();
      } catch (err) {
        await stream.close().catch(() => {});
        // Refused, perhaps because the session no longer acts as System Admin (or a SET
        // ROLE that failed half way): ask it at the next read.
        if (this.#systemAdmin || mayChangeRole(sql)) this.#askSystemAdmin = true;
        throw err;
      }
      this.noteStatement(sql);
      return stream;
    });
    opening.catch(() => {}); // the caller sees the failure through the methods below
    return {
      get statementId() {
        return submitted?.statementId;
      },
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
    // "none" after a statement that ended the transaction is the truth (a stale read lags toward
    // "open"); right after one that opened it, it may be stale (noteStatement).
    if (doc.transaction === "none" && this.#endedSinceOpen) {
      this.#maybeOpen = false;
      this.#endedSinceOpen = false;
    }
    const options = doc.options ?? {};
    this.#readOnly ||= options["read_only"] === true;
    if (this.#askSystemAdmin) {
      const was = this.#systemAdmin;
      try {
        this.#systemAdmin = await actsAsSystemAdmin(conn);
        this.#askSystemAdmin = false;
      } catch (err) {
        if (isSessionGone(err)) throw err;
        // Not one now. A refusal as System Admin means the session asked for it and doesn't
        // act as one (revoked, or the store did not answer), which the server may undo (a
        // grant again, the store back) until RESET ROLE: asked at every read until it answers.
        // Anything else answers nothing (an aborted transaction; a server without System
        // Admin): asked again only if it was one.
        this.#systemAdmin = false;
        this.#askSystemAdmin = was || isSystemAdminRefusal(err);
      }
    }
    this.#info = {
      id: doc.session_id,
      serverId: this.#serverId,
      role: this.#role,
      readOnly: this.#readOnly,
      systemAdmin: this.#systemAdmin,
      transaction: doc.transaction,
      catalog: typeof options["catalog"] === "string" ? options["catalog"] : "",
      schema: typeof options["schema"] === "string" ? options["schema"] : "",
      idleTimeoutS: doc.idle_timeout_s,
    };
    return this.#info;
  }

  /**
   * Makes the open session read-only (one way), returning the session as the server then
   * describes it. A refusal the server gives with the session intact (temporary sequences, a
   * transaction with work in it, a server without read-only sessions) leaves it exactly as it
   * was, still open and read-write, and throws ReadOnlyRefusedError with the server's words.
   * Only when the outcome is unknown (no answer, a session the server no longer has, an
   * answer that doesn't confirm it) does the tab stop using the session: it is closed and the
   * tab is blocked until New session, with a message that says why.
   */
  async enableReadOnly(where: Where): Promise<SessionInfo> {
    const conn = await this.connection({ ...where, readOnly: false });
    return this.#readOnly ? this.#read(conn) : this.#switchReadOnly(conn);
  }

  async #switchReadOnly(conn: Connection): Promise<SessionInfo> {
    // A session acting as System Admin that turns read-only stops acting as one: ask after.
    if (this.#systemAdmin) this.#askSystemAdmin = true;
    try {
      await conn.setOptions({ read_only: true });
    } catch (err) {
      if (isReadOnlyRefusal(err)) {
        // The session is as it was: show it as the server now describes it (a read that fails
        // leaves the last one; the next run finds out what happened to the session).
        await this.#read(conn).catch(() => {});
        throw new ReadOnlyRefusedError(err);
      }
      throw await this.#unknownMode(conn, err);
    }
    let info: SessionInfo;
    try {
      info = await this.#read(conn);
    } catch (err) {
      throw await this.#unknownMode(conn, err);
    }
    if (!info.readOnly) {
      throw await this.#unknownMode(conn, new Error("The server did not confirm read-only mode."));
    }
    return info;
  }

  /** The switch's outcome is unknown: stop using the session, and say why. */
  async #unknownMode(conn: Connection, err: unknown): Promise<Error> {
    const gone = isSessionGone(err) || ChargewireError.is(err, ErrorCode.sessionEnded);
    const why = err instanceof Error ? err.message.replace(/[.\s]+$/, "") : String(err);
    this.#modeFailure = new Error(
      gone
        ? `The session ended on the server before read-only could be confirmed (${why}). Its temporary ` +
            "tables, settings and any open transaction are gone. Choose New session to continue."
        : `Whether the server made the session read-only is not known (${why}). To be safe this tab no ` +
            "longer uses it: the session was closed, with its temporary tables, settings and any open " +
            "transaction. Choose New session to continue.",
    );
    this.#lost();
    this.#info = undefined;
    await conn.close().catch(() => {});
    return this.#modeFailure;
  }

  /** Sets the session's catalog, schema and role now, if it is open (else at its opening). */
  async setWhere(where: Where): Promise<void> {
    if (!this.#conn) return;
    const options: Record<string, string> = {};
    if (where.catalog) options["catalog"] = where.catalog;
    if (where.schema) options["schema"] = where.schema;
    const role = where.role ?? "";
    // Moving the session never changes its mode (a replacement opens as openReadOnly asks, and
    // read-only when this one was).
    await this.run({ ...where, readOnly: false }, async (conn) => {
      if (Object.keys(options).length) {
        try {
          await conn.setOptions(options);
        } catch (err) {
          if (where.catalog && isCatalogRefused(err)) {
            await this.#read(conn);
            throw new CatalogRefusedError(where.catalog);
          }
          throw err;
        }
      }
      if (role !== this.#role) {
        this.#askSystemAdmin = true;
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
    this.#readOnly = false;
    this.#modeFailure = undefined;
    this.#maybeOpen = false; // ending it rolls back what was open
    this.#endedSinceOpen = false;
    this.#settingsChanged = false;
    this.#systemAdmin = false;
    this.#askSystemAdmin = false;
    if (conn) await conn.then((c) => c.close()).catch(() => {});
  }

  #lost(): void {
    const conn = this.#conn;
    this.#conn = undefined;
    this.#maybeOpen = false; // a new session starts in no transaction
    this.#endedSinceOpen = false;
    this.#settingsChanged = false;
    this.#systemAdmin = false;
    this.#askSystemAdmin = false;
    // The server has no session to end; this only releases the client's side.
    if (conn) void conn.then((c) => c.close()).catch(() => {});
  }
}
