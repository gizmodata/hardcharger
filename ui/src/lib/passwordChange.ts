// A temporary password: one someone else set for the user (a help desk's reset, or an
// administrator's `... WITH PASSWORD '...' EXPIRE`). The server lets a session signed in with
// it run one statement only, the user's own change, and refuses everything else saying so.
// The UI notices that refusal when it first asks who it is, and asks for a new password.

import { errorMessage } from "../session";

/** What every refusal of a session with a temporary password says (server/src/authz/authz.cc). */
const mustChangeWords = "must be changed before anything else runs";

/** Was this error the server holding the session until its password is changed? */
export function passwordMustChange(err: unknown): boolean {
  return errorMessage(err).includes(mustChangeWords);
}

/**
 * Was it held because the password is older than the server's password_max_age_days (rather
 * than set by someone else)? The server says "has expired (password_max_age_days)".
 */
export function passwordExpired(err: unknown): boolean {
  return passwordMustChange(err) && errorMessage(err).includes("has expired (password_max_age_days)");
}

/** A SQL string literal: the text between single quotes, each quote doubled. */
export function sqlString(text: string): string {
  return `'${text.replaceAll("'", "''")}'`;
}

/** The statement that changes the session's own password, proven with the current one. */
export function changeOwnPasswordSql(newPassword: string, currentPassword: string): string {
  return `ALTER USER CURRENT_USER WITH PASSWORD ${sqlString(newPassword)} REPLACE ${sqlString(currentPassword)}`;
}
