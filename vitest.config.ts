import { defineConfig } from "vitest/config";

export default defineConfig({
  define: { __UI_VERSION__: JSON.stringify("test") },
  test: {
    include: ["test/unit/**/*.test.{ts,tsx}"],
    environment: "jsdom",
    // A run in which no test ran is a failure, not a pass.
    passWithNoTests: false,
  },
});
