import { defineConfig, loadEnv } from "vite";
export default defineConfig(({ mode, command }) => {
  const env = loadEnv(mode, process.cwd(), "");
  if (command === "build" && (mode === "mock" || env.VITE_CONSOLE_MOCK === "1"))
    throw new Error("Production mock is forbidden");
  return {
    base: "/console/",
    define: {
      __CONSOLE_MOCK__: JSON.stringify(command === "serve" && mode === "mock"),
    },
    build: { assetsInlineLimit: 0, sourcemap: false },
    server: {
      port: 5173,
      strictPort: true,
      proxy: {
        "/console/v1": {
          target: env.CONSOLE_BACKEND ?? "http://127.0.0.1:8766",
          changeOrigin: false,
        },
      },
    },
  };
});
