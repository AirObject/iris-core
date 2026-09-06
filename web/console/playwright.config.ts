import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "tests/browser",
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:8766",
    headless: true,
    launchOptions: process.env.CONSOLE_BROWSER_EXECUTABLE
      ? { executablePath: process.env.CONSOLE_BROWSER_EXECUTABLE }
      : {},
  },
  reporter: [["list"]],
  outputDir: "test-results",
  webServer: {
    command:
      "cd ../.. && .venv/bin/python web/console/tests/backend/serve_fixture.py",
    url: "http://127.0.0.1:8766/console/",
    reuseExistingServer: !process.env.CI,
    timeout: 30000,
  },
});
