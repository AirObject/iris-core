import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "../src/iris/web", emptyOutDir: true },
  server: { proxy: { "/admin/api": "http://127.0.0.1:8080" } },
  test: { environment: "jsdom", setupFiles: "./src/test-setup.ts" },
});
