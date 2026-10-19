// The signed-in session the whole app shares: the access token, a Warp SQL connection for
// metadata and small queries, who the server says we are, and the way back to the sign-in
// page when the token expires or an administrator ends the session.

import { type Connection, type DiscoveryDocument, ErrorCode, WarpSqlError } from "@gizmodata/warp-sql";
import { createContext, use } from "react";

import type { SignedIn } from "./lib/auth";
import type { ConnectionOptions } from "./lib/warp";

export interface Identity {
  user: string;
  role: string;
  version: string;
  sessionId: string;
  instanceId: string | null;
}

export interface QueryRequest {
  sql: string;
  run: boolean;
  newTab: boolean;
  title?: string;
}

/** What the query page offers the rest of the app while it is mounted. */
export interface QueryPageHandle {
  open: (request: QueryRequest) => void;
  insert: (text: string) => void;
}

export interface AppSession {
  auth: SignedIn;
  server: DiscoveryDocument;
  connection: Connection;
  identity: Identity;
  isAdmin: boolean;
  /** Opens a new connection (one per editor tab, so one tab's query never waits for another's). */
  openConnection: (options?: ConnectionOptions) => Promise<Connection>;
  /** Ends the session and goes back to the sign-in page, saying why. */
  signOut: (why?: string) => void;
  /** Checks an error for an expired or ended sign-in; returns true when it handled it. */
  checkSignIn: (err: unknown) => boolean;
  /** Shows the query page with a statement in a tab (and maybe runs it). */
  requestQuery: (request: QueryRequest) => void;
  /** Inserts text into the active editor at its cursor. */
  insertText: (text: string) => void;
  /** The query page registers itself while mounted; returns the way to unregister. */
  registerQueryPage: (handle: QueryPageHandle) => () => void;
}

export const SessionContext = createContext<AppSession | null>(null);

export function useSession(): AppSession {
  const session = use(SessionContext);
  if (!session) throw new Error("useSession outside a signed-in session");
  return session;
}

/** Why the sign-in no longer works, if `err` says so (Warp SQL §4.2, §5.6). */
export function signInLost(err: unknown): string | undefined {
  if (!WarpSqlError.is(err)) return undefined;
  if (err.code === ErrorCode.invalidToken) return "Your sign-in has expired. Sign in again.";
  if (err.code === ErrorCode.sessionEnded) {
    switch (err.reason) {
      case "killed":
        return "An administrator ended your session. Sign in again.";
      case "credentials_changed":
        return "Your password or credentials changed. Sign in again.";
      case "principal_dropped":
        return "Your user no longer exists on this server.";
      default:
        return "Your sign-in has expired. Sign in again.";
    }
  }
  return undefined;
}

/** The message to show for an error: the server's detail, without the client's suffix. */
export function errorMessage(err: unknown): string {
  if (WarpSqlError.is(err)) {
    if (err.code === ErrorCode.statementCancelled) return "The query was cancelled.";
    if (err.code === ErrorCode.transportError) return "The server could not be reached.";
    return err.detail ?? err.message;
  }
  if (err instanceof Error) return err.message;
  return String(err);
}
