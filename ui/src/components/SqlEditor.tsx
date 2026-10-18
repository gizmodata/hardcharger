import { useEffect, useRef } from "react";

import { monaco } from "../lib/monaco";
import { statementAt } from "../lib/sql";

export interface EditorApi {
  /** The selection if there is one, else the statement under the cursor (with its range). */
  current: () => { sql: string; range: monaco.IRange } | undefined;
  insert: (text: string) => void;
  focus: () => void;
  highlight: (range: monaco.IRange | undefined) => void;
}

interface Props {
  value: string;
  onChange: (value: string) => void;
  onRun: () => void;
  theme: "light" | "dark";
  onReady: (api: EditorApi | undefined) => void;
}

/** The SQL editor (Monaco). Ctrl/⌘+Enter runs the selection or the statement under the cursor. */
export function SqlEditor({ value, onChange, onRun, theme, onReady }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const editor = useRef<monaco.editor.IStandaloneCodeEditor | undefined>(undefined);
  const latest = useRef({ onChange, onRun });
  useEffect(() => {
    latest.current = { onChange, onRun };
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
      placeholder: "Write SQL here. Ctrl/⌘+Enter runs the statement under the cursor (or the selection).",
      ariaLabel: "SQL editor",
      fixedOverflowWidgets: true,
    });
    editor.current = ed;
    const changes = ed.onDidChangeModelContent(() => latest.current.onChange(ed.getValue()));
    ed.addAction({
      id: "hardcharger.run",
      label: "Run statement",
      keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter],
      contextMenuGroupId: "navigation",
      contextMenuOrder: 0,
      run: () => latest.current.onRun(),
    });
    let decorations = ed.createDecorationsCollection();
    const api: EditorApi = {
      current: () => {
        const model = ed.getModel();
        const selection = ed.getSelection();
        if (!model || !selection) return undefined;
        if (!selection.isEmpty()) {
          const sql = model.getValueInRange(selection);
          return sql.trim() ? { sql, range: selection } : undefined;
        }
        const text = model.getValue();
        const s = statementAt(text, model.getOffsetAt(selection.getPosition()));
        if (!s) return undefined;
        const a = model.getPositionAt(s.start);
        const b = model.getPositionAt(s.end);
        return { sql: s.text, range: new monaco.Range(a.lineNumber, a.column, b.lineNumber, b.column) };
      },
      insert: (text) => {
        const selection = ed.getSelection();
        if (!selection) return;
        ed.executeEdits("hardcharger", [{ range: selection, text, forceMoveMarkers: true }]);
        ed.focus();
      },
      focus: () => ed.focus(),
      highlight: (range) => {
        decorations.clear();
        if (range) {
          decorations = ed.createDecorationsCollection([
            { range, options: { className: "editor-running", isWholeLine: false } },
          ]);
        }
      },
    };
    onReady(api);
    return () => {
      onReady(undefined);
      changes.dispose();
      ed.dispose();
      editor.current = undefined;
    };
    // The editor is created once per tab; value and theme changes go through the effects below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    monaco.editor.setTheme(theme === "dark" ? "hardcharger-dark" : "hardcharger-light");
  }, [theme]);

  // A value set from outside (a table preview, a restored tab) replaces the text.
  useEffect(() => {
    const ed = editor.current;
    if (ed && ed.getValue() !== value) ed.setValue(value);
  }, [value]);

  return <div ref={host} className="editor-host" data-testid="sql-editor" />;
}
