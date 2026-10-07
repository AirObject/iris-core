import { useEffect, useState, useCallback } from "react";

let csrf = "";
export function setCSRF(value: string) {
  csrf = value;
}

export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public status: number,
  ) {
    super(message);
  }
}
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/admin/api${path}`, {
    ...init,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      ...(csrf ? { "X-Iris-CSRF": csrf } : {}),
      ...init?.headers,
    },
  });
  const body = await response.json();
  if (!response.ok) {
    const error = body.error || {};
    if (
      ["login_required", "setup_required", "csrf_failed"].includes(error.code)
    )
      dispatchEvent(new Event("iris-auth"));
    throw new ApiError(
      error.code || "request_failed",
      error.message ||
        (error.fields
          ? `请检查输入：${error.fields.map((f: { field: string; message: string }) => f.field.split(".").pop()).join("、")}`
          : "请求失败，请重试"),
      response.status,
    );
  }
  return body;
}
export const json = (method: string, body?: unknown): RequestInit => ({
  method,
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});
export const errorText = (error: unknown) =>
  error instanceof ApiError
    ? error.message
    : "无法连接服务，请检查服务是否仍在运行";

export function useData<T>(path: string | null, interval = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [version, setVersion] = useState(0);
  const refresh = useCallback(() => setVersion((v) => v + 1), []);
  useEffect(() => {
    setData(null);
    setError("");
  }, [path]);
  useEffect(() => {
    if (path === null) return;
    const controller = new AbortController();
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const result = await api<T>(path, { signal: controller.signal });
        if (active) {
          setData(result);
          setError("");
        }
      } catch (e) {
        if (active && !controller.signal.aborted) setError(errorText(e));
      }
      if (active && interval) timer = setTimeout(load, interval);
    };
    void load();
    return () => {
      active = false;
      controller.abort();
      clearTimeout(timer);
    };
  }, [path, interval, version]);
  return { data, error, refresh };
}
