import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./test",
  use: { baseURL: "http://127.0.0.1:1420", channel: process.env.CI ? "chrome" : undefined, viewport: { width: 1120, height: 880 } },
  webServer: { command: "npm run dev", url: "http://127.0.0.1:1420", reuseExistingServer: !process.env.CI },
});
