import { useEffect, useRef } from "react";

import { type CompletionTarget, registerCompletionTarget, registerSqlCompletion } from "../lib/completion";
import { monaco } from "../lib/monaco";
import { sqlNameDragType } from "../lib/sqlNameDrag";
import { keepTypedIndentation } from "../lib/typedIndent";
import { splitStatements, statementAt } from "../lib/sql";

export interface EditorApi {
  /**
   * What Run executes: the selected text when there is a selection (split into its statements),
   * else the statement the cursor is in. The range is what gets highlighted while it runs.
   */
  current: () => { statements: string[]; range: monaco.IRange } | undefined;
  /** Every statement in the editor, for Run all. */
  all: () => { statements: string[]; range: monaco.IRange } | undefined;
  insert: (text: string) => void;
  /** The editor's text now (the tab's copy follows it a render later). */
  value: () => string;
  focus: () => void;
  highlight: (range: monaco.IRange | undefined) => void;
}

export interface EditorCommands {
  onRun: () => void;
  onRunAll: () => void;
  onNewTab?: () => void;
  onCloseTab?: () => void;
  onSave?: () => void;
}

interface Props extends EditorCommands {
  /** What autocomplete lists for this editor (the tab's listings, catalog and schema). */
  completion?: () => CompletionTarget | undefined;
  value: string;
  onChange: (value: string) => void;
  theme: "light" | "dark";
  onReady: (api: EditorApi | undefined) => void;
}

/**
 * The SQL editor (Monaco). Its shortcuts are editor actions, not addCommand: Monaco keeps
 * addCommand keybindings in a service every editor shares, so with an editor per tab the
 * last one mounted would answer Ctrl/⌘+Enter in all of them. An action's keybinding fires only
 * in its own focused editor. Ctrl+… also works on macOS (other SQL tools' habit).
 */
export function SqlEditor({ value, onChange, theme, onReady, completion, ...commands }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const editor = useRef<monaco.editor.IStandaloneCodeEditor | undefined>(undefined);
  const latest = useRef({ onChange, commands, completion });
  /** What the editor reported through onChange that has not come back as `value` yet. */
  const echoes = useRef<string[]>([]);
  useEffect(() => {
    latest.current = { onChange, commands, completion };
  });

  useEffect(() => {
    const el = host.current!;
    const ed = monaco.editor.create(el, {
      value,
      language: "sql",
      theme: theme === "dark" ? "hardcharger-dark" : "hardcharger-light",
      automaticLayout: true,
      minimap: { enabled: false },
      fontSize: 13,
      fontFamily: "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace",
      scrollBeyondLastLine: false,
      tabSize: 2,
      // Enter keeps the line's indentation and nothing more (no language rules), and pasted
      // text goes in as it is (keepTypedIndentation below: typed or pasted indentation
      // replaces the kept one rather than adding to it).
      autoIndent: "keep",
      formatOnPaste: false,
      formatOnType: false,
      autoIndentOnPaste: false,
      // Enter takes a suggestion only when it changes the text: a word typed out in full,
      // then Enter, is a new line, not the same word put in again.
      acceptSuggestionOnEnter: "smart",
      wordWrap: "on",
      padding: { top: 8, bottom: 8 },
      renderLineHighlight: "line",
      placeholder: "Write SQL here. Ctrl/⌘+Enter runs the statement at the cursor (or the selection).",
      ariaLabel: "SQL editor",
      fixedOverflowWidgets: true,
      // The margin bar that marks the statement Run executes needs the decorations lane.
      lineDecorationsWidth: 8,
      // Suggestions come from the server (catalogs, tables, columns, keywords), not from words
      // already in the text.
      wordBasedSuggestions: "off",
      // Names dragged from the schema browser are inserted by the editor's own drop handler.
      dropIntoEditor: { enabled: false },
    });
    editor.current = ed;
    const model = ed.getModel()!;
    const indentation = keepTypedIndentation(ed);
    // Autocomplete: one provider for every editor; this model follows this tab.
    registerSqlCompletion();
    const stopCompletion = registerCompletionTarget(model, () => latest.current.completion?.());

    // A name dragged from the schema browser goes in as plain text where it is dropped
    // (capture phase, ahead of Monaco's own listeners).
    const dom = ed.getContainerDomNode();
    const onDragOver = (e: DragEvent) => {
      if (!e.dataTransfer?.types.includes(sqlNameDragType)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
    };
    const onDrop = (e: DragEvent) => {
      const text = e.dataTransfer?.getData(sqlNameDragType);
      if (!text) return;
      e.preventDefault();
      e.stopPropagation();
      const at = ed.getTargetAtClientPoint(e.clientX, e.clientY)?.position ?? ed.getPosition();
      if (!at) return;
      ed.executeEdits("schema-browser", [
        { range: new monaco.Range(at.lineNumber, at.column, at.lineNumber, at.column), text, forceMoveMarkers: true },
      ]);
      ed.pushUndoStop();
      ed.setPosition({ lineNumber: at.lineNumber, column: at.column + text.length });
      ed.focus();
    };
    dom.addEventListener("dragover", onDragOver, true);
    dom.addEventListener("drop", onDrop, true);
    const changes = ed.onDidChangeModelContent(() => {
      const text = ed.getValue();
      // Each is acknowledged at a later render. Many keys can come before one (a script types
      // a hundred in a few milliseconds); the cap only keeps memory bounded if none ever is.
      const e = echoes.current;
      e.push(text);
      let size = 0;
      for (let i = e.length - 1; i >= 0; i--) {
        size += e[i]!.length;
        if (size > 8_000_000 || e.length - i > 2000) {
          e.splice(0, i + 1);
          break;
        }
      }
      latest.current.onChange(text);
    });
    const { KeyMod, KeyCode } = monaco;
    const action = (id: string, label: string, keys: number[], run: () => void, menu = false) =>
      ed.addAction({
        id,
        label,
        keybindings: keys,
        run,
        ...(menu ? { contextMenuGroupId: "navigation", contextMenuOrder: 0 } : {}),
      });
    action(
      "hardcharger.run",
      "Run statement",
      [KeyMod.CtrlCmd | KeyCode.Enter, KeyMod.WinCtrl | KeyCode.Enter],
      () => latest.current.commands.onRun(),
      true,
    );
    action(
      "hardcharger.runAll",
      "Run all",
      [KeyMod.CtrlCmd | KeyMod.Shift | KeyCode.Enter, KeyMod.WinCtrl | KeyMod.Shift | KeyCode.Enter],
      () => latest.current.commands.onRunAll(),
      true,
    );
    // Alt+N / Alt+W: Ctrl/⌘+T and Ctrl/⌘+W belong to the browser.
    action("hardcharger.newTab", "New tab", [KeyMod.Alt | KeyCode.KeyN], () => latest.current.commands.onNewTab?.());
    action("hardcharger.closeTab", "Close tab", [KeyMod.Alt | KeyCode.KeyW], () =>
      latest.current.commands.onCloseTab?.(),
    );
    action("hardcharger.save", "Save query", [KeyMod.CtrlCmd | KeyCode.KeyS, KeyMod.WinCtrl | KeyCode.KeyS], () =>
      latest.current.commands.onSave?.(),
    );

    const rangeOf = (start: number, end: number) => {
      const a = model.getPositionAt(start);
      const b = model.getPositionAt(end);
      return new monaco.Range(a.lineNumber, a.column, b.lineNumber, b.column);
    };

    // The margin bar on the statement Run would execute, when there is a choice.
    const mark = ed.createDecorationsCollection();
    const markCurrent = () => {
      const text = model.getValue();
      const position = ed.getPosition();
      const selection = ed.getSelection();
      if (!position || (selection && !selection.isEmpty()) || splitStatements(text).length < 2) {
        mark.clear();
        return;
      }
      const s = statementAt(text, model.getOffsetAt(position));
      if (!s) {
        mark.clear();
        return;
      }
      const a = model.getPositionAt(s.start);
      const b = model.getPositionAt(s.end);
      mark.set([
        {
          range: new monaco.Range(a.lineNumber, 1, b.lineNumber, 1),
          options: { isWholeLine: true, linesDecorationsClassName: "editor-current-statement" },
        },
      ]);
    };
    // Outside Monaco's own event delivery: changing decorations from inside a cursor or
    // content listener fed Monaco's view events back into it (a stack overflow on a long paste).
    let pendingMark = 0;
    const scheduleMark = () => {
      if (pendingMark) return;
      pendingMark = requestAnimationFrame(() => {
        pendingMark = 0;
        if (editor.current === ed) markCurrent();
      });
    };
    const cursor = ed.onDidChangeCursorSelection(scheduleMark);
    const content = ed.onDidChangeModelContent(scheduleMark);
    markCurrent();

    let running = ed.createDecorationsCollection();
    const api: EditorApi = {
      current: () => {
        const selection = ed.getSelection();
        if (!selection) return undefined;
        if (!selection.isEmpty()) {
          const text = model.getValueInRange(selection);
          const statements = splitStatements(text).map((s) => s.text);
          return statements.length ? { statements, range: selection } : undefined;
        }
        const s = statementAt(model.getValue(), model.getOffsetAt(selection.getPosition()));
        return s ? { statements: [s.text], range: rangeOf(s.start, s.end) } : undefined;
      },
      all: () => {
        const found = splitStatements(model.getValue());
        if (!found.length) return undefined;
        return { statements: found.map((s) => s.text), range: rangeOf(found[0]!.start, found.at(-1)!.end) };
      },
      insert: (text) => {
        const selection = ed.getSelection();
        if (!selection) return;
        ed.executeEdits("hardcharger", [{ range: selection, text, forceMoveMarkers: true }]);
        ed.pushUndoStop();
        ed.focus();
      },
      value: () => model.getValue(),
      focus: () => ed.focus(),
      highlight: (range) => {
        running.clear();
        if (range) {
          running = ed.createDecorationsCollection([{ range, options: { className: "editor-running" } }]);
        }
      },
    };
    onReady(api);
    return () => {
      onReady(undefined);
      stopCompletion();
      dom.removeEventListener("dragover", onDragOver, true);
      dom.removeEventListener("drop", onDrop, true);
      cancelAnimationFrame(pendingMark);
      changes.dispose();
      cursor.dispose();
      content.dispose();
      indentation.dispose();
      ed.dispose();
      editor.current = undefined;
    };
    // The editor is created once per tab; value and theme changes go through the effects below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    monaco.editor.setTheme(theme === "dark" ? "hardcharger-dark" : "hardcharger-light");
  }, [theme]);

  // A value set from outside (a table preview, a saved query) replaces the text. The editor's
  // own text comes back here too, after React renders, and may by then be older than what is
  // in the editor (a key pressed between the render and this effect): that is an echo, never
  // a reason to put the older text back (it lost keystrokes and moved the cursor).
  useEffect(() => {
    const ed = editor.current;
    if (!ed) return;
    const echo = echoes.current.lastIndexOf(value);
    if (echo >= 0) {
      echoes.current.splice(0, echo + 1);
      return;
    }
    if (ed.getValue() !== value) ed.setValue(value);
    // (setValue reported the new text too; it is this value, not an echo to wait for.)
    echoes.current = [];
  }, [value]);

  return <div ref={host} className="editor-host" data-testid="sql-editor" />;
}
