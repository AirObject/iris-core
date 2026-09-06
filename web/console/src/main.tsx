import { createRoot } from "react-dom/client";
import { App } from "./App";
import { api } from "./api/client";
import "./styles.css";
async function start() {
  if (__CONSOLE_MOCK__) {
    const { createMockTransport } = await import("./mock/server");
    api.transport = createMockTransport();
  }
  createRoot(document.getElementById("root")!).render(<App />);
}
void start();
