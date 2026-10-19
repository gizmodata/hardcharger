import { defineConfig } from "vitest/config";

export default defineConfig({
  define: { __UI_VERSION__: JSON.stringify("test"), __BUILD_YEAR__: JSON.stringify("1999") },
  test: {
    include: ["test/unit/**/*.test.{ts,tsx}"],
    environment: "jsdom",
    // A run in which no test ran is a failure, not a pass.
    passWithNoTests: false,
  },
});
