// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import { mayTakeFocus } from "../../src/lib/focus";

describe("mayTakeFocus", () => {
  it("doesn't take focus from a field the person is typing in, and takes it from anything else", () => {
    document.body.innerHTML = `
      <input id="search" /><textarea id="notes"></textarea><select id="pick"></select>
      <div id="rich" contenteditable="true"></div><button id="tab">Query 2</button>
      <div id="editor" class="editor-host"><textarea id="inside"></textarea></div>
      <div id="other" class="editor-host"><textarea id="hidden-tab"></textarea></div>`;
    const el = (id: string) => document.getElementById(id);
    const editor = el("editor");
    for (const id of ["search", "notes", "pick", "rich"]) {
      expect(mayTakeFocus(el(id), editor), id).toBe(false);
    }
    expect(mayTakeFocus(el("tab"), editor)).toBe(true); // a tab clicked: the editor may focus
    expect(mayTakeFocus(document.body, editor)).toBe(true); // nothing focused
    expect(mayTakeFocus(null, editor)).toBe(true);
    expect(mayTakeFocus(el("inside"), editor)).toBe(true); // already in the editor
    // Another tab's editor (WebKit keeps its text area focused when the tab is hidden).
    expect(mayTakeFocus(el("hidden-tab"), editor)).toBe(true);
  });
});
