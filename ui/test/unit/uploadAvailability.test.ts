// Upload is offered only where its session can write (src/lib/upload/availability.ts): the
// editor tab's Upload, the schema browser's Upload button and "Upload data…" items, and a
// dropped file all ask this.
import { describe, expect, it } from "vitest";

import {
  uploadOffInReadOnlyTab,
  uploadOffOnReadOnlyConnection,
  uploadUnavailable,
} from "../../src/lib/upload/availability";

describe("uploadUnavailable", () => {
  it("a connection saved with Read-only sessions: not offered, with the reason", () => {
    expect(uploadUnavailable({ connectionReadOnly: true })).toBe(
      "Upload is off: this connection opens read-only sessions",
    );
    expect(uploadOffOnReadOnlyConnection).toBe("Upload is off: this connection opens read-only sessions");
    // Twin: a read-write connection offers it.
    expect(uploadUnavailable({ connectionReadOnly: false })).toBeUndefined();
  });

  it("an entry that follows the active tab: a read-only tab turns it off, on any connection", () => {
    expect(uploadUnavailable({ connectionReadOnly: false, tabReadOnly: true })).toBe(
      "Upload is off in a read-only tab",
    );
    expect(uploadOffInReadOnlyTab).toBe("Upload is off in a read-only tab");
    // On a read-only connection, its setting is the reason given.
    expect(uploadUnavailable({ connectionReadOnly: true, tabReadOnly: true })).toBe(uploadOffOnReadOnlyConnection);
    // Twin: a read-write tab on a read-write connection offers it.
    expect(uploadUnavailable({ connectionReadOnly: false, tabReadOnly: false })).toBeUndefined();
  });
});
