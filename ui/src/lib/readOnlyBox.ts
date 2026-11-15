// What an editor tab's Read-only checkbox shows. It tells the truth about the session: checked
// and locked only once the server has said, for the session the tab has open, that it is
// read-only; pending (never checked) while that is being asked; and, with no session open, the
// tab's choice for the next one, which the server confirms before anything runs in it.

export interface ReadOnlyBoxInput {
  /** A session is open (or opening): TabSession.open. */
  open: boolean;
  /** The open session's read_only as the server last described it; undefined: not read. */
  sessionReadOnly: boolean | undefined;
  /**
   * The server confirmed read-only for this tab's session (TabSession.readOnly): the open
   * one, or with none open the last one, whose replacement opens read-only too.
   */
  confirmed: boolean;
  /** The tab's choice for new sessions: its own, else the connection's default. */
  requested: boolean;
  /** The connection's default for new sessions ("Read-only sessions" when it was added). */
  connectionDefault: boolean;
  /** A switch of the open session is waiting for the server's answer. */
  switching: boolean;
  /** A run started that asked for a read-only session not yet confirmed (one opening). */
  runAskedReadOnly: boolean;
  running: boolean;
  inTransaction: boolean;
}

export type ReadOnlyBoxState = "on" | "off" | "pending" | "next-on" | "next-off";

export interface ReadOnlyBox {
  state: ReadOnlyBoxState;
  checked: boolean;
  /** Shown as neither checked nor unchecked: an answer is awaited. */
  pending: boolean;
  disabled: boolean;
  note: string;
}

const lockedNote = "Locked for this session. Choose New session to change.";

/**
 * What a run (or the session's opening) asks for. An open session keeps the mode the server
 * gave it: the connection's default is for sessions opened later, and a tick on an open session
 * switches it (TabSession.enableReadOnly), never silently.
 */
export function runReadOnly(open: boolean, confirmed: boolean, requested: boolean): boolean {
  return open ? confirmed : requested || confirmed;
}

export function readOnlyBox(i: ReadOnlyBoxInput): ReadOnlyBox {
  const on = i.open && (i.sessionReadOnly === true || i.confirmed);
  if (on) return { state: "on", checked: true, pending: false, disabled: true, note: lockedNote };
  if (i.switching || (i.running && i.runAskedReadOnly)) {
    return { state: "pending", checked: false, pending: true, disabled: true, note: "Confirming with the server…" };
  }
  if (i.open) {
    return {
      state: "off",
      checked: false,
      pending: false,
      disabled: i.running || i.inTransaction,
      note: i.inTransaction
        ? "Finish the transaction before enabling read-only."
        : i.connectionDefault
          ? "This session is read-write. New sessions on this connection start read-only."
          : "Once enabled, stays on until a new session.",
    };
  }
  // No session: the choice for the next one.
  if (i.confirmed) {
    return {
      state: "next-on",
      checked: true,
      pending: false,
      disabled: true,
      note: "The next session opens read-only too, as this tab's last one was. Choose New session to change.",
    };
  }
  return {
    state: i.requested ? "next-on" : "next-off",
    checked: i.requested,
    pending: false,
    disabled: i.running,
    note: i.requested
      ? "The next session opens read-only, once the server confirms it."
      : "Once enabled, stays on until a new session.",
  };
}
