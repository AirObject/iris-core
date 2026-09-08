import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "tests/statistics", testMatch: "statistics.spec.ts", workers: 1,
  use: { baseURL: "http://127.0.0.1:8787", headless: true,
    launchOptions: process.env.CONSOLE_BROWSER_EXECUTABLE ? { executablePath: process.env.CONSOLE_BROWSER_EXECUTABLE } : {} },
  reporter: [["list"]], outputDir: "test-results/statistics",
  webServer: { command: "cd ../.. && PYTHONPATH=src:sdk/python/src .venv/bin/python web/console/tests/backend/serve_statistics_fixture.py",
    url: "http://127.0.0.1:8787/console/", reuseExistingServer: false, timeout: 30000 },
});
