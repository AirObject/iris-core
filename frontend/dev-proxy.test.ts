// @vitest-environment node
import { createServer as httpServer } from "node:http";
import { once } from "node:events";
import { createServer as viteServer } from "vite";
import { expect, test } from "vitest";
import configuration from "./vite.config";

test("development proxy preserves browser origin for the admin CSRF check", async () => {
  const backend = httpServer((request, response) => {
    const sameOrigin =
      request.headers.origin === `http://${request.headers.host}`;
    const credentials =
      request.headers.cookie === "iris_session=fake-session" &&
      request.headers["x-iris-csrf"] === "fake-csrf";
    response.writeHead(sameOrigin && credentials ? 200 : 403, {
      "Content-Type": "application/json",
    });
    response.end(JSON.stringify({ sameOrigin, credentials }));
  });
  backend.listen(0, "127.0.0.1");
  await once(backend, "listening");
  const address = backend.address();
  if (!address || typeof address === "string")
    throw new Error("missing listener");
  const target = `http://127.0.0.1:${address.port}`;
  const proxy = configuration.server!.proxy!["/admin/api"];
  const vite = await viteServer({
    ...configuration,
    configFile: false,
    logLevel: "silent",
    server: {
      ...configuration.server,
      host: "127.0.0.1",
      port: 0,
      proxy: {
        "/admin/api": typeof proxy === "string" ? target : { ...proxy, target },
      },
    },
  });
  try {
    await vite.listen();
    const frontend = vite.httpServer!.address();
    if (!frontend || typeof frontend === "string")
      throw new Error("missing frontend");
    const origin = `http://127.0.0.1:${frontend.port}`;
    const response = await fetch(`${origin}/admin/api/setup/password`, {
      method: "POST",
      headers: {
        Origin: origin,
        Cookie: "iris_session=fake-session",
        "X-Iris-CSRF": "fake-csrf",
        "Content-Type": "application/json",
      },
      body: "{}",
    });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      sameOrigin: true,
      credentials: true,
    });
  } finally {
    await vite.close();
    backend.closeAllConnections();
    await new Promise<void>((resolve) => backend.close(() => resolve()));
  }
});
