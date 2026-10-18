// Monaco, trimmed to what a SQL editor needs (the full editor.main also loads every
// language and the TypeScript, CSS, HTML and JSON services). Its one worker is a file of
// its own (worker-src 'self'); nothing here needs eval or inline scripts.

import "monaco-editor/editor/browser/coreCommands.js";
import "monaco-editor/editor/contrib/bracketMatching/browser/bracketMatching.js";
import "monaco-editor/editor/contrib/clipboard/browser/clipboard.js";
import "monaco-editor/editor/contrib/comment/browser/comment.js";
import "monaco-editor/editor/contrib/contextmenu/browser/contextmenu.js";
import "monaco-editor/editor/contrib/cursorUndo/browser/cursorUndo.js";
import "monaco-editor/editor/contrib/dnd/browser/dnd.js";
import "monaco-editor/editor/contrib/dropOrPasteInto/browser/copyPasteContribution.js";
import "monaco-editor/features/find/register.js";
import "monaco-editor/editor/contrib/find/browser/findController.js";
import "monaco-editor/editor/contrib/folding/browser/folding.js";
import "monaco-editor/editor/contrib/hover/browser/hoverContribution.js";
import "monaco-editor/editor/contrib/linesOperations/browser/linesOperations.js";
import "monaco-editor/editor/contrib/multicursor/browser/multicursor.js";
import "monaco-editor/editor/contrib/placeholderText/browser/placeholderText.contribution.js";
import "monaco-editor/editor/contrib/snippet/browser/snippetController2.js";
import "monaco-editor/editor/contrib/suggest/browser/suggestController.js";
import "monaco-editor/editor/contrib/wordHighlighter/browser/wordHighlighter.js";
import "monaco-editor/editor/contrib/wordOperations/browser/wordOperations.js";
import "monaco-editor/editor/common/standaloneStrings.js";
import "monaco-editor-esm/base/browser/ui/codicons/codicon/codicon.css";
import "monaco-editor-esm/base/browser/ui/codicons/codicon/codicon-modifiers.css";
import "monaco-editor/languages/definitions/sql/register.js";

import * as monaco from "monaco-editor/editor/editor.api.js";
import EditorWorker from "monaco-editor/editor/editor.worker.js?worker";

import { completionNames } from "./names";

self.MonacoEnvironment = {
  getWorker: () => new EditorWorker(),
};

monaco.editor.defineTheme("hardcharger-light", {
  base: "vs",
  inherit: true,
  rules: [
    { token: "keyword", foreground: "b93d0e", fontStyle: "bold" },
    { token: "string", foreground: "1a6f96" },
    { token: "number", foreground: "7a3e9d" },
    { token: "comment", foreground: "566966", fontStyle: "italic" },
  ],
  colors: { "editor.background": "#ffffff", "editorLineNumber.foreground": "#566966" },
});

monaco.editor.defineTheme("hardcharger-dark", {
  base: "vs-dark",
  inherit: true,
  rules: [
    { token: "keyword", foreground: "f15a24", fontStyle: "bold" },
    { token: "string", foreground: "31a8db" },
    { token: "number", foreground: "c9a3e6" },
    { token: "comment", foreground: "8fa3a0", fontStyle: "italic" },
  ],
  colors: {
    "editor.background": "#0b1416",
    "editorLineNumber.foreground": "#8fa3a0",
    "editor.lineHighlightBackground": "#101f26",
  },
});

const keywords = `SELECT FROM WHERE GROUP BY ORDER HAVING LIMIT OFFSET JOIN LEFT RIGHT FULL INNER OUTER CROSS
  ON USING AS AND OR NOT IN IS NULL LIKE ILIKE BETWEEN CASE WHEN THEN ELSE END DISTINCT ALL UNION
  INTERSECT EXCEPT WITH RECURSIVE INSERT INTO VALUES UPDATE SET DELETE CREATE REPLACE TABLE VIEW
  SCHEMA DATABASE DROP ALTER ADD COLUMN INDEX IF EXISTS PRIMARY KEY FOREIGN REFERENCES DEFAULT
  CAST TRY_CAST QUALIFY WINDOW OVER PARTITION ROWS RANGE PIVOT UNPIVOT DESCRIBE SUMMARIZE SHOW
  EXPLAIN ANALYZE ATTACH DETACH USE COPY TO EXPORT IMPORT BEGIN COMMIT ROLLBACK TRANSACTION
  GRANT REVOKE ROLE USER COUNT SUM AVG MIN MAX ASC DESC NULLS FIRST LAST TRUE FALSE`
  .split(/\s+/)
  .filter(Boolean);

monaco.languages.registerCompletionItemProvider("sql", {
  provideCompletionItems(model, position) {
    const word = model.getWordUntilPosition(position);
    const range = new monaco.Range(position.lineNumber, word.startColumn, position.lineNumber, word.endColumn);
    return {
      suggestions: [
        ...keywords.map((k) => ({ label: k, kind: monaco.languages.CompletionItemKind.Keyword, insertText: k, range })),
        ...[...completionNames].map((n) => ({
          label: n,
          kind: monaco.languages.CompletionItemKind.Field,
          insertText: n,
          range,
        })),
      ],
    };
  },
});

export { monaco };
