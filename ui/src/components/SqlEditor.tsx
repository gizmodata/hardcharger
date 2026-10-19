import { useEffect, useRef } from "react";

import { monaco } from "../lib/monaco";
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
export function SqlEditor({ value, onChange, theme, onReady, ...commands }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const editor = useRef<monaco.editor.IStandaloneCodeEditor | undefined>(undefined);
  const latest = useRef({ onChange, commands });
  useEffect(() => {
    latest.current = { onChange, commands };
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
    const changes = ed.onDidChangeModelContent(() => latest.current.onChange(ed.getValue()));
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
      cancelAnimationFrame(pendingMark);
      changes.dispose();
      cursor.dispose();
      content.dispose();
      ed.dispose();
      editor.current = undefined;
    };
    // The editor is created once per tab; value and theme changes go through the effects below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    monaco.editor.setTheme(theme === "dark" ? "hardcharger-dark" : "hardcharger-light");
  }, [theme]);

  // A value set from outside (a table preview, a saved query) replaces the text.
  useEffect(() => {
    const ed = editor.current;
    if (ed && ed.getValue() !== value) ed.setValue(value);
  }, [value]);

  return <div ref={host} className="editor-host" data-testid="sql-editor" />;
}
