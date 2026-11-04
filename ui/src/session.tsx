// The signed-in session the whole app shares: the access token, a Chargewire connection for
// metadata and small queries, who the server says we are, and the way back to the sign-in
// page when the token expires or an administrator ends the session.

import { type Connection, type DiscoveryDocument, ErrorCode, ChargewireError } from "@gizmodata/chargewire";
import { createContext, use } from "react";

import type { SignedIn } from "./lib/auth";
import type { ConnectionOptions } from "./lib/chargewire";
import type { AppContract, ServersStore } from "./lib/servers";

export interface Identity {
  user: string;
  role: string;
  version: string;
  sessionId: string;
  instanceId: string | null;
  fullAdmin?: boolean;
  instrumentationEnabled?: boolean;
}

export interface QueryRequest {
  sql: string;
  run: boolean;
  newTab: boolean;
  title?: string;
  /** The server the tab is bound to (a saved server's id; "" or absent: this server). */
  server?: string;
  /** The new tab's catalog and schema (a table preview opens where the table is). */
  catalog?: string;
  schema?: string;
  /** A saved query: open it in the tab that has it, or in a new tab that edits it. */
  saved?: { id: string; sql: string };
}

/** What the query page offers the rest of the app while it is mounted. */
export interface QueryPageHandle {
  open: (request: QueryRequest) => void;
  insert: (text: string) => void;
  /** Points the active tab at a catalog (and schema); says why not when it can't. */
  pointTabAt: (catalog: string, schema?: string, server?: string) => string | undefined;
}

export interface AppSession {
  /** The UI's own sign-in; a placeholder (empty token) without a local server. */
  auth: SignedIn;
  server: DiscoveryDocument;
  /** The UI's own session on this server; rejects everything without a local server. */
  connection: Connection;
  identity: Identity;
  isAdmin: boolean;
  /** What serves the UI: a Hardcharger server to sign in to, or the UI alone (`hardcharger ui`). */
  contract: AppContract;
  /** False in the UI without a server: no "This server", no admin pages, remotes only. */
  localServer: boolean;
  /** The other servers this tab knows and is signed in to, and the way to reach them. */
  servers: ServersStore;
  /**
   * Opens a new connection (one per editor tab, so one tab's query never waits for
   * another's): on this server, or on the saved server `options.server` names.
   */
  openConnection: (options?: ConnectionOptions & { server?: string }) => Promise<Connection>;
  /** Ends the session and goes back to the sign-in page, saying why. */
  signOut: (why?: string) => void;
  /** Checks an error for an expired or ended sign-in; returns true when it handled it. */
  checkSignIn: (err: unknown) => boolean;
  /** Shows the query page with a statement in a tab (and maybe runs it). */
  requestQuery: (request: QueryRequest) => void;
  /** Inserts text into the active editor at its cursor. */
  insertText: (text: string) => void;
  /** Points the active editor tab at a catalog (and schema); says why not when it can't. */
  pointTabAt: (catalog: string, schema?: string, server?: string) => string | undefined;
  /** The query page registers itself while mounted; returns the way to unregister. */
  registerQueryPage: (handle: QueryPageHandle) => () => void;
}

export const SessionContext = createContext<AppSession | null>(null);

export function useSession(): AppSession {
  const session = use(SessionContext);
  if (!session) throw new Error("useSession outside a signed-in session");
  return session;
}

/** Why the sign-in no longer works, if `err` says so (Chargewire §4.2, §5.6). */
export function signInLost(err: unknown): string | undefined {
  if (!ChargewireError.is(err)) return undefined;
  if (err.code !== ErrorCode.invalidToken && err.code !== ErrorCode.sessionEnded) return undefined;
  // The reasons of §4.2 and §5.6; an unknown one (or none) reads as an expired sign-in.
  switch (err.reason) {
    case "idle_in_transaction":
      // The sign-in stands: the server ended this one session for holding a transaction open
      // with nothing running (idle_in_transaction_session_timeout) and rolled it back. The
      // error's own detail says so, next to the statement that found the session gone.
      return undefined;
    case "killed":
      return "An administrator ended your session. Sign in again.";
    case "credentials_changed":
      return "Your password or credentials changed. Sign in again.";
    case "principal_dropped":
      return "Your user no longer exists on this server.";
    case "token_revoked":
      return "Your sign-in was revoked. Sign in again.";
    default:
      return "Your sign-in has expired. Sign in again.";
  }
}

/** The message to show for an error: the server's detail, without the client's suffix. */
export function errorMessage(err: unknown): string {
  if (ChargewireError.is(err)) {
    if (err.code === ErrorCode.statementCancelled) return "The query was cancelled.";
    if (err.code === ErrorCode.statementTimeout) return "The query ran longer than its time limit.";
    if (err.code === ErrorCode.clientTransportError) return "The server could not be reached.";
    return err.detail ?? err.message;
  }
  if (err instanceof Error) return err.message;
  return String(err);
}
