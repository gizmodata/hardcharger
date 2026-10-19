// "Run as": under role-based access control a tab can act with one of the user's roles (SQL
// SET ROLE), so it sees and may do only what that role grants; "All my roles" (RESET ROLE) is
// the default. The roles come from SHOW GRANTS FOR USER (its role memberships), on the app's
// own session. An administrator holds every right already (the server refuses SET ROLE for one), and
// a server without access control has no roles: neither gets a picker.

import { type Connection, ErrorCode, WarpSqlError } from "@gizmodata/warp-sql";

import { quoteIdentifier } from "./sql";

/** The roles granted to the signed-in user, or none (no picker). */
export async function grantedRoles(conn: Connection, user: string, isAdmin: boolean): Promise<string[]> {
  if (isAdmin) return [];
  let table;
  try {
    table = await conn.query(`SHOW GRANTS FOR USER ${quoteIdentifier(user)}`);
  } catch {
    return []; // no access control: no roles to pick
  }
  const roles = new Set<string>();
  for (const row of table) {
    const r = (row as { toJSON: () => Record<string, unknown> }).toJSON();
    if (r["privilege"] === "ROLE" && r["object_type"] === "ROLE" && typeof r["object"] === "string") {
      roles.add(r["object"]);
    }
  }
  return [...roles].sort((a, b) => a.localeCompare(b));
}

/** The statement that makes a session act with `role` ("" for all of the user's roles). */
export function setRoleSql(role: string): string {
  return role ? `SET ROLE ${quoteIdentifier(role)}` : "RESET ROLE";
}

/**
 * A connection that acts with one role, for listing what the role sees (the schema browser and
 * autocomplete). Its session is opened without silent replacement: a replacement would act
 * with all the user's roles. When the server has lost it, the next call opens a new one.
 */
export class RoleConnection {
  readonly #open: () => Promise<Connection>;
  readonly #role: string;
  #conn: Promise<Connection> | undefined;

  constructor(role: string, open: () => Promise<Connection>) {
    this.#role = role;
    this.#open = open;
  }

  get(): Promise<Connection> {
    if (!this.#conn) {
      const opening = this.#open().then(async (conn) => {
        await conn.execute(setRoleSql(this.#role));
        return conn;
      });
      opening.catch(() => {
        if (this.#conn === opening) this.#conn = undefined;
      });
      this.#conn = opening;
    }
    return this.#conn;
  }

  /** Runs `work`, once more on a new session if the server lost this one. */
  async run<T>(work: (conn: Connection) => Promise<T>): Promise<T> {
    try {
      return await work(await this.get());
    } catch (err) {
      if (!(WarpSqlError.is(err) && err.code === ErrorCode.sessionNotFound)) throw err;
      this.#conn = undefined;
      return work(await this.get());
    }
  }

  async close(): Promise<void> {
    const conn = this.#conn;
    this.#conn = undefined;
    if (conn) await conn.then((c) => c.close()).catch(() => {});
  }
}
