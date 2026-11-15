// The Read-only checkbox (EditorTab, from readOnlyBox): checked and locked only when the server
// said the open session is read-only; pending, never checked, while it is being asked; with no
// session, the tab's choice for the next one. One case per way the tab's request can change
// without the server's word, each beside its positive twin.
import { describe, expect, it } from "vitest";

import { type ReadOnlyBoxInput, readOnlyBox, runReadOnly } from "../../src/lib/readOnlyBox";

const base: ReadOnlyBoxInput = {
  open: false,
  sessionReadOnly: undefined,
  confirmed: false,
  requested: false,
  connectionDefault: false,
  switching: false,
  runAskedReadOnly: false,
  running: false,
  inTransaction: false,
};
const box = (patch: Partial<ReadOnlyBoxInput>) => readOnlyBox({ ...base, ...patch });

describe("the Read-only box", () => {
  it("add or edit server: the connection's default turns on over an open read-write session", () => {
    // The read-write session is open; the dialog saved "Read-only sessions" for the connection.
    const now = { open: true, sessionReadOnly: false, requested: true, connectionDefault: true };
    expect(box(now)).toMatchObject({ state: "off", checked: false, pending: false, disabled: false });
    expect(box(now).note).toBe("This session is read-write. New sessions on this connection start read-only.");
    // A run on it asks for the mode the session has: the open session is not silently claimed.
    expect(runReadOnly(true, false, true)).toBe(false);
    // Twin: once the server says this session is read-only, the box is checked and locked.
    expect(box({ ...now, sessionReadOnly: true, confirmed: true })).toMatchObject({
      state: "on",
      checked: true,
      disabled: true,
      note: "Locked for this session. Choose New session to change.",
    });
  });

  it("a tick on an open session: pending while the server is asked, unchecked and usable after a refusal", () => {
    const open = { open: true, sessionReadOnly: false };
    expect(box({ ...open, switching: true })).toMatchObject({
      state: "pending",
      checked: false,
      pending: true,
      disabled: true,
      note: "Confirming with the server…",
    });
    // Refused with the session intact: as it was before the tick.
    expect(box(open)).toMatchObject({ state: "off", checked: false, disabled: false });
    // Twin: confirmed.
    expect(box({ ...open, sessionReadOnly: true, confirmed: true })).toMatchObject({ state: "on", checked: true });
  });

  it("restored tabs after a reload: the stored choice is a request until the new session's server says so", () => {
    const restored = { requested: true };
    expect(box(restored)).toMatchObject({ state: "next-on", checked: true, disabled: false });
    expect(box(restored).note).toBe("The next session opens read-only, once the server confirms it.");
    expect(runReadOnly(false, false, true)).toBe(true); // the new session is asked to be read-only
    // The first run: pending, not checked, until the session is confirmed.
    expect(box({ ...restored, running: true, runAskedReadOnly: true, open: true })).toMatchObject({
      state: "pending",
      checked: false,
    });
    expect(box({ ...restored, open: true, sessionReadOnly: true, confirmed: true })).toMatchObject({ state: "on" });
    // A stored read-write choice on a server that makes sessions read-only anyway: the truth.
    expect(box({ requested: false, open: true, sessionReadOnly: true, confirmed: true })).toMatchObject({
      state: "on",
      checked: true,
    });
  });

  it("automatic session replacement: shown as the server describes the new session", () => {
    // A read-only session replaced (no transaction was open): the replacement opens read-only
    // (TabSession carries the restriction), and the box stays locked.
    expect(runReadOnly(false, true, false)).toBe(true);
    expect(box({ open: true, confirmed: true, sessionReadOnly: true })).toMatchObject({ state: "on" });
    // A read-write one replaced while the connection's default is now read-only opens read-only
    // (TabSession: openReadOnly); until the tab has read it, the box claims nothing.
    expect(box({ open: true, sessionReadOnly: undefined, requested: true, connectionDefault: true })).toMatchObject({
      state: "off",
      checked: false,
    });
    expect(box({ open: true, sessionReadOnly: true, confirmed: true, requested: true })).toMatchObject({
      state: "on",
      checked: true,
    });
  });

  it("a read-only session lost with a transaction open: the next one is read-only too, and the box can't be unticked", () => {
    const lost = { confirmed: true, requested: false };
    expect(box(lost)).toMatchObject({ state: "next-on", checked: true, disabled: true });
    expect(runReadOnly(false, true, false)).toBe(true);
    // Twin: after New session (TabSession.close() clears it) the choice is free again.
    expect(box({ requested: false })).toMatchObject({ state: "next-off", checked: false, disabled: false });
  });

  it("New session: the box shows the tab's choice for the next one, not the old session's state", () => {
    expect(box({ requested: true })).toMatchObject({ state: "next-on", checked: true, disabled: false });
    expect(box({ requested: false })).toMatchObject({ state: "next-off", checked: false, disabled: false });
  });

  it("a second tab on the same server: its own session, its own answer", () => {
    // A new tab inherits the connection's default; its first run is pending until confirmed.
    const second = { requested: true, connectionDefault: true };
    expect(box(second)).toMatchObject({ state: "next-on", checked: true });
    expect(box({ ...second, running: true, runAskedReadOnly: true })).toMatchObject({
      state: "pending",
      checked: false,
    });
    // The first tab's read-write session is unaffected by the second's.
    expect(box({ open: true, sessionReadOnly: false, connectionDefault: true, requested: true })).toMatchObject({
      checked: false,
    });
  });

  it("an open session whose last read failed is never shown as the request", () => {
    // The tab asks for read-only (the connection's default) but the session's state is unknown.
    expect(box({ open: true, sessionReadOnly: undefined, requested: true, connectionDefault: true })).toMatchObject({
      state: "off",
      checked: false,
      disabled: false, // a tick asks the server, which decides
    });
    // Twin: the server confirmed it earlier (TabSession.readOnly).
    expect(box({ open: true, sessionReadOnly: undefined, confirmed: true })).toMatchObject({ state: "on" });
  });

  it("a transaction or a run locks the choice without claiming anything", () => {
    expect(box({ open: true, sessionReadOnly: false, inTransaction: true })).toMatchObject({
      checked: false,
      disabled: true,
      note: "Finish the transaction before enabling read-only.",
    });
    expect(box({ open: true, sessionReadOnly: false, running: true })).toMatchObject({
      checked: false,
      disabled: true,
    });
    expect(box({ running: true, requested: false })).toMatchObject({ checked: false, disabled: true });
  });
});
