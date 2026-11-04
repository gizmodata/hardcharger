// @ts-check
import js from "@eslint/js";
import prettier from "eslint-config-prettier";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import { defineConfig } from "eslint/config";
import tseslint from "typescript-eslint";

export default defineConfig(
  { ignores: ["dist/", "release/", "node_modules/", "test-results/", "playwright-report/", "coverage/", "vendor/"] },
  js.configs.recommended,
  ...tseslint.configs.strictTypeChecked,
  {
    languageOptions: {
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
      globals: { ...globals.browser },
    },
    rules: {
      "@typescript-eslint/restrict-template-expressions": [
        "error",
        { allowNumber: true, allowBoolean: true, allowNullish: true },
      ],
      "@typescript-eslint/no-unnecessary-condition": "off",
      "@typescript-eslint/no-confusing-void-expression": "off",
      "@typescript-eslint/no-non-null-assertion": "off",
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
      // The desktop app's webview has no window.confirm, prompt or alert (confirm answers
      // false at once): use lib/dialogs.ts, the page's own dialogs.
      "no-alert": "error",
      "no-restricted-properties": [
        "error",
        ...["confirm", "prompt", "alert"].flatMap((property) =>
          ["window", "globalThis", "self"].map((object) => ({
            object,
            property,
            message: "Native dialogs don't exist in the desktop app's webview: use lib/dialogs.ts.",
          })),
        ),
      ],
    },
  },
  {
    files: ["src/**/*.tsx"],
    plugins: { "react-hooks": reactHooks, "react-refresh": reactRefresh },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": ["error", { allowConstantExport: true }],
    },
  },
  {
    files: ["test/**", "scripts/**", "*.config.*"],
    languageOptions: { globals: { ...globals.node } },
    rules: {
      "@typescript-eslint/no-unsafe-assignment": "off",
      "@typescript-eslint/no-unsafe-member-access": "off",
      "@typescript-eslint/no-unsafe-argument": "off",
      "@typescript-eslint/no-unsafe-call": "off",
      "@typescript-eslint/no-unsafe-return": "off",
      "@typescript-eslint/require-await": "off",
      "@typescript-eslint/no-floating-promises": "off",
      "@typescript-eslint/no-misused-promises": "off",
      "no-empty-pattern": "off", // Playwright fixtures take `{}` when they use no other fixture
    },
  },
  // Plain JavaScript (scripts) and the build configs: no type information here. The configs
  // are type-checked by `npm run typecheck` (tsconfig.node.json).
  { files: ["**/*.mjs", "*.config.ts"], ...tseslint.configs.disableTypeChecked },
  prettier,
);
