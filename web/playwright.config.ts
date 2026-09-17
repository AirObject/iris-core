import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  outputDir: process.env.IRIS_BROWSER_OUTPUT ?? '/tmp/iris-browser-results',
  timeout: 120_000,
  workers: 1,
  retries: 0,
  use: {
    baseURL: process.env.IRIS_BROWSER_URL ?? 'http://127.0.0.1:18080',
    viewport: { width: 390, height: 844 },
    trace: 'off',
    screenshot: 'only-on-failure',
  },
});
