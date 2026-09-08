import type { Envelope, Meta, Result, Session } from "./design";
import protocol from "./protocol.json" with { type: "json" };
export const CONTRACT_VERSION = protocol.contract_version as Meta["contract_version"];
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    public kind: string,
    public details: Record<string, unknown> = {},
    public retryable = false,
    public requestId = "",
    public retryAfter = 0,
  ) {
    super(`${code}${kind ? ` / ${kind}` : ""}`);
  }
}
export type Transport = (
  path: string,
  init: RequestInit,
  progress?: (loaded: number, total: number) => void,
) => Promise<Response>;
export const browserTransport: Transport = (path, init, progress) => {
  if (!progress) return fetch(path, init);
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(init.method ?? "PUT", path);
    xhr.withCredentials = true;
    new Headers(init.headers).forEach((value, key) =>
      xhr.setRequestHeader(key, value),
    );
    xhr.upload.onprogress = (event) => progress(event.loaded, event.total);
    xhr.onload = () => {
      cleanup();
      const headers = new Headers();
      xhr
        .getAllResponseHeaders()
        .trim()
        .split(/[\r\n]+/)
        .filter(Boolean)
        .forEach((line) => {
          const i = line.indexOf(":");
          headers.append(line.slice(0, i), line.slice(i + 1).trim());
        });
      resolve(
        new Response(xhr.status === 204 ? null : xhr.responseText, {
          status: xhr.status,
          headers,
        }),
      );
    };
    xhr.onerror = () => {
      cleanup();
      reject(new ApiError(0, "network_error", "upload_interrupted"));
    };
    xhr.onabort = () => {
      cleanup();
      reject(new DOMException("Aborted", "AbortError"));
    };
    const abort = () => xhr.abort();
    const cleanup = () => init.signal?.removeEventListener("abort", abort);
    init.signal?.addEventListener("abort", abort, { once: true });
    if (init.signal?.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    xhr.send(init.body as Blob);
  });
};
export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH";
  body?: unknown;
  key?: string;
  signal?: AbortSignal;
  raw?: Blob;
  contentType?: string;
  progress?: (loaded: number, total: number) => void;
  userActivity?: boolean;
  stream?: boolean;
  noRetry?: boolean;
}
const pause = (ms: number, signal?: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    const abort = () => {
      clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", abort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) abort();
  });
const retryDelay = (header: string | null) => {
  if (!header) return 0;
  const seconds = Number(header);
  return Number.isFinite(seconds)
    ? Math.max(0, seconds * 1000)
    : Math.max(0, Date.parse(header) - Date.now());
};
export class ConsoleClient {
  session: Session | null = null;
  epoch = 0;
  private refreshFlight: Promise<void> | null = null;
  private restoreFlight: Promise<void> | null = null;
  private reauthFlight: Promise<void> | null = null;
  private listeners = new Set<() => void>();
  private controllers = new Set<AbortController>();
  private actionFlights = new Map<string, Promise<Result<unknown>>>();
  onReauth: (() => Promise<void>) | null = null;
  constructor(public transport: Transport = browserTransport) {}
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private emit() {
    this.listeners.forEach((fn) => fn());
  }
  clear() {
    this.session = null;
    this.epoch++;
    this.controllers.forEach((c) => c.abort());
    this.controllers.clear();
    this.actionFlights.clear();
    this.emit();
  }
  private acceptSession(session: Session) {
    const old = this.session;
    const changed =
      !old ||
      old.session.id !== session.session.id ||
      old.operator.key_id !== session.operator.key_id ||
      JSON.stringify(old.grants) !== JSON.stringify(session.grants) ||
      JSON.stringify(old.permissions) !== JSON.stringify(session.permissions);
    this.session = session;
    if (changed) {
      this.epoch++;
      this.actionFlights.clear();
    }
    this.emit();
  }
  async login(key: string) {
    this.clear();
    const reply = await this.request<Session>("/auth/login", {
      method: "POST",
      body: { key },
      noRetry: true,
    });
    this.acceptSession(reply.data);
  }
  async restore() {
    if (this.restoreFlight) return this.restoreFlight;
    this.restoreFlight = (async () => {
      const reply = await this.request<Session>("/auth/session", {
        noRetry: true,
      });
      this.acceptSession(reply.data);
    })().finally(() => {
      this.restoreFlight = null;
    });
    return this.restoreFlight;
  }
  async reauth(key: string) {
    await this.request("/auth/reauth", {
      method: "POST",
      body: { key },
      noRetry: true,
    });
  }
  async logout() {
    try {
      await this.request("/auth/logout", {
        method: "POST",
        body: {},
        noRetry: true,
      });
    } finally {
      this.clear();
    }
  }
  async activity() {
    if (!this.session) return;
    if (Date.parse(this.session.session.expires_at) <= Date.now()) {
      this.clear();
      throw new ApiError(401, "access_denied", "authentication_required");
    }
    if (this.refreshFlight) return this.refreshFlight;
    if (Date.parse(this.session.session.idle_expires_at) - Date.now() > 120000)
      return;
    const key = crypto.randomUUID();
    this.refreshFlight = (async () => {
      let lostResponseRetried = false;
      for (;;) {
        try {
          const reply = await this.request<Session>("/auth/refresh", {
            method: "POST",
            body: {},
            key,
          });
          this.acceptSession(reply.data);
          return;
        } catch (error) {
          if (
            error instanceof ApiError &&
            error.status === 0 &&
            !lostResponseRetried
          ) {
            lostResponseRetried = true;
            continue;
          }
          throw error;
        }
      }
    })().finally(() => {
      this.refreshFlight = null;
    });
    return this.refreshFlight;
  }
  action<T>(
    path: string,
    options: RequestOptions & { key: string },
  ): Promise<Result<T>> {
    const previous = this.actionFlights.get(options.key);
    if (previous) return previous as Promise<Result<T>>;
    const promise = this.request<T>(path, options).finally(() => {
      this.actionFlights.delete(options.key);
    });
    this.actionFlights.set(options.key, promise);
    return promise;
  }
  async request<T>(
    path: string,
    options: RequestOptions = {},
  ): Promise<Result<T>> {
    if (
      !path.startsWith("/") ||
      path.startsWith("//") ||
      path.includes("..") ||
      path.includes("#")
    )
      throw new Error("仅接受 Console 相对 API 路径");
    const auth = path.startsWith("/auth/");
    if (options.userActivity && !auth) await this.activity();
    if (this.refreshFlight && !auth) await this.refreshFlight;
    if (
      this.session &&
      Date.parse(this.session.session.expires_at) <= Date.now()
    ) {
      this.clear();
      throw new ApiError(401, "access_denied", "authentication_required");
    }
    const epoch = this.epoch;
    const controller = new AbortController();
    this.controllers.add(controller);
    const signal = options.signal
      ? AbortSignal.any([controller.signal, options.signal])
      : controller.signal;
    const method = options.method ?? "GET";
    const writing = method !== "GET";
    const exempt =
      path === "/auth/login" ||
      path === "/auth/reauth" ||
      path === "/auth/logout" ||
      /^\/auth\/sessions\/[^/]+:revoke$/.test(path);
    const actionKey =
      writing && !exempt ? (options.key ?? crypto.randomUUID()) : undefined;
    let csrfRetried = false,
      reauthRetried = false,
      retries = 0;
    try {
      for (;;) {
        if (signal.aborted) throw new DOMException("Aborted", "AbortError");
        const headers = new Headers({
          Accept: "application/json",
          "X-IMC-Console": "1",
        });
        if (writing) {
          headers.set(
            "Content-Type",
            options.contentType ?? "application/json",
          );
          if (path !== "/auth/login" && this.session)
            headers.set("X-IMC-CSRF", this.session.csrf_token);
          if (actionKey) headers.set("Idempotency-Key", actionKey);
        }
        let response: Response;
        try {
          response = await this.transport(
            `/console/v1${path}`,
            {
              method,
              credentials: "same-origin",
              cache: "no-store",
              headers,
              body: writing
                ? (options.raw ?? JSON.stringify(options.body ?? {}))
                : undefined,
              signal,
            },
            options.progress,
          );
        } catch (error) {
          if (error instanceof DOMException && error.name === "AbortError")
            throw error;
          throw new ApiError(0, "network_error", "response_unknown", {}, false);
        }
        if (response.ok) {
          if (epoch !== this.epoch && !auth)
            throw new DOMException("Session changed", "AbortError");
          if (options.stream)
            return {
              data: response as T,
              meta: {
                request_id: response.headers.get("X-Request-ID") ?? "",
                contract_version: CONTRACT_VERSION,
                as_of: "",
              },
              status: response.status,
            };
          if (response.status === 204)
            return {
              data: undefined as T,
              meta: {
                request_id: response.headers.get("X-Request-ID") ?? "",
                contract_version: CONTRACT_VERSION,
                as_of: "",
              },
              status: 204,
            };
          const envelope = (await response.json()) as Envelope<T>;
          return { ...envelope, status: response.status };
        }
        const payload = (await response.json().catch(() => ({}))) as {
          error?: {
            code?: string;
            details?: Record<string, unknown>;
            retryable?: boolean;
          };
          request_id?: string;
        };
        const details = payload.error?.details ?? {};
        const error = new ApiError(
          response.status,
          payload.error?.code ?? "http_error",
          String(details.kind ?? ""),
          details,
          payload.error?.retryable ?? false,
          payload.request_id ?? response.headers.get("X-Request-ID") ?? "",
          retryDelay(response.headers.get("Retry-After")),
        );
        if (error.status === 401) {
          if (path !== "/auth/login" && path !== "/auth/reauth") this.clear();
          throw error;
        }
        if (
          error.code === "access_denied" &&
          error.kind === "csrf_failed" &&
          !csrfRetried &&
          path !== "/auth/login" &&
          path !== "/auth/session"
        ) {
          csrfRetried = true;
          await this.restore();
          if (this.epoch !== epoch) throw error;
          continue;
        }
        if (
          error.code === "access_denied" &&
          error.kind === "reauth_required" &&
          !auth &&
          !reauthRetried &&
          this.onReauth
        ) {
          reauthRetried = true;
          this.reauthFlight ??= this.onReauth().finally(() => {
            this.reauthFlight = null;
          });
          await this.reauthFlight;
          if (this.epoch !== epoch) throw error;
          continue;
        }
        if (
          error.code === "access_denied" &&
          error.kind === "permission_denied" &&
          !auth
        ) {
          await this.restore().catch(() => {});
        }
        if (
          error.retryable &&
          !options.noRetry &&
          retries < 2 &&
          (error.status === 429 ||
            error.status >= 500 ||
            error.kind === "idempotency_in_progress")
        ) {
          await pause(Math.max(error.retryAfter, 500 * 2 ** retries), signal);
          retries++;
          continue;
        }
        throw error;
      }
    } finally {
      this.controllers.delete(controller);
    }
  }
  async download(path: string, filename: string) {
    const { data: response } = await this.request<Response>(path, {
      stream: true,
      userActivity: true,
    });
    const epoch = this.epoch;
    type SaveWindow = Window & {
      showSaveFilePicker?: (options: { suggestedName: string }) => Promise<{
        createWritable: () => Promise<WritableStream<Uint8Array>>;
      }>;
    };
    const picker = (window as SaveWindow).showSaveFilePicker;
    if (picker && response.body) {
      const handle = await picker({ suggestedName: filename });
      const writable = await handle.createWritable();
      await response.body.pipeTo(writable);
      return;
    }
    const blob = await response.blob();
    if (epoch !== this.epoch)
      throw new DOMException("Session changed", "AbortError");
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}
export const api = new ConsoleClient();
export function metadataText(meta: Meta) {
  return [
    meta.request_id && `请求 ${meta.request_id}`,
    meta.source && `来源 ${meta.source}`,
    meta.computed_at && `数据截至 ${meta.computed_at}`,
    meta.coverage_from && `覆盖起点 ${meta.coverage_from}`,
    meta.stale && "数据已落后",
    meta.rollup_lag_us && `汇总延迟 ${meta.rollup_lag_us} 微秒`,
    meta.partial && "部分统计",
    meta.instance_local && "此服务实例",
    meta.approximate && "近似值",
    ...(meta.warnings ?? []),
  ]
    .filter(Boolean)
    .join(" · ");
}
