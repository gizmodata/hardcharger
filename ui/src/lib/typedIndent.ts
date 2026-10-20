// Enter keeps the line's indentation (autoIndent "keep"), but that indentation is the
// editor's guess. When the next thing typed on the new line is indentation of its own (a
// space), or a paste that starts with some, it replaces the guess instead of adding to it.
// Without this, text typed line by line (a script, a remote desktop's "type clipboard", a
// person re-typing an indented statement) was indented once more on every line.

import { monaco } from "./monaco";

interface Guess {
  line: number;
  /** The indentation Enter put in: the whole line, the cursor at its end. */
  width: number;
  /** The model's version right after Enter; any other edit since retires the guess. */
  version: number;
}

export function keepTypedIndentation(ed: monaco.editor.IStandaloneCodeEditor): monaco.IDisposable {
  const model = ed.getModel()!;
  let enter = false;
  let guess: Guess | undefined;

  /** The guess, if it still stands: nothing edited since, the cursor at its end. */
  const standing = (version: number): Guess | undefined => {
    if (!guess || guess.version !== version) return undefined;
    const selections = ed.getSelections() ?? [];
    if (selections.length !== 1) return undefined;
    return guess;
  };

  const keys = ed.onKeyDown((e) => {
    const plain = !e.ctrlKey && !e.metaKey && !e.altKey;
    if (e.keyCode === monaco.KeyCode.Enter) {
      enter = plain && !e.shiftKey;
      return;
    }
    enter = false;
    if (e.keyCode !== monaco.KeyCode.Space || !plain || e.shiftKey) return;
    const g = standing(model.getVersionId());
    const at = ed.getSelection();
    if (!g || !at?.isEmpty() || at.positionLineNumber !== g.line || at.positionColumn !== g.width + 1) return;
    if (model.getLineContent(g.line).length !== g.width) return;
    e.preventDefault();
    e.stopPropagation();
    guess = undefined;
    ed.executeEdits(
      "keyboard",
      [{ range: new monaco.Range(g.line, 1, g.line, g.width + 1), text: " " }],
      [new monaco.Selection(g.line, 2, g.line, 2)],
    );
  });

  const changes = model.onDidChangeContent((e) => {
    // (A guess is kept through other edits: its version retires it, and a paste checks it
    // was the one edit since.)
    const wasEnter = enter;
    enter = false;
    if (!wasEnter || e.isUndoing || e.isRedoing || e.isFlush || e.changes.length !== 1) return;
    const indent = /^\r?\n([ \t]+)$/.exec(e.changes[0]!.text);
    if (!indent) return;
    const line = e.changes[0]!.range.startLineNumber + 1;
    const width = indent[1]!.length;
    if (model.getLineContent(line).length !== width) return;
    guess = { line, width, version: e.versionId };
  });

  // A paste at the guessed indentation that brings its own: the guess goes. (The paste is in
  // by now, the one edit since the guess.)
  const paste = ed.onDidPaste((e) => {
    const g = guess;
    guess = undefined;
    if (!g || model.getVersionId() !== g.version + 1) return;
    if (e.range.startLineNumber !== g.line || e.range.startColumn !== g.width + 1) return;
    const first = model.getValueInRange(new monaco.Range(g.line, g.width + 1, g.line, g.width + 2));
    if (first !== " " && first !== "\t") return;
    ed.executeEdits("paste", [{ range: new monaco.Range(g.line, 1, g.line, g.width + 1), text: "" }]);
  });

  return {
    dispose: () => {
      keys.dispose();
      changes.dispose();
      paste.dispose();
    },
  };
}
