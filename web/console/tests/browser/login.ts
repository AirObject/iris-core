import { setTimeout as delay } from "node:timers/promises";
import { expect, type Page } from "@playwright/test";

/** Retry at most twice after the real server-directed login cooldown. */
export async function loginWithCooldown(page: Page, token: string) {
  const credential = page.getByLabel("运营密钥", { exact: true });
  const submit = page.getByRole("button", { name: "登录控制台", exact: true });
  const attempt = async () => {
    await credential.fill(token);
    const response = page.waitForResponse(result => result.url().endsWith("/console/v1/auth/login"));
    await submit.click();
    return response;
  };
  let response = await attempt();
  for (let retry = 0; response.status() === 429 && retry < 2; retry += 1) {
    const seconds = Number(response.headers()["retry-after"]);
    expect(Number.isInteger(seconds)).toBe(true);
    expect(seconds).toBeGreaterThan(0);
    expect(seconds).toBeLessThanOrEqual(60);
    // Address and key fixed windows can expire at different times.
    // The UI clears the credential after every attempt; its disabled button
    // is not a cooldown indicator. Honor Retry-After without polling.
    await delay(seconds * 1000 + 100);
    response = await attempt();
  }
  expect(response.status()).toBe(200);
}
