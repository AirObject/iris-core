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
export async function apiResponse<T>(
  path: string,
  init?: RequestInit,
): Promise<{ data: T; status: number; headers: Headers }> {
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
          ? `请检查输入：${error.fields.map((f: { field: string; message: string }) => `${f.field.split(".").pop()}：${f.message}`).join("；")}`
          : "请求失败，请重试"),
      response.status,
    );
  }
  return { data: body, status: response.status, headers: response.headers };
}
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  return (await apiResponse<T>(path, init)).data;
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
  const [loading, setLoading] = useState(path !== null);
  const [version, setVersion] = useState(0);
  const refresh = useCallback(() => {
    setLoading(true);
    setVersion((v) => v + 1);
  }, []);
  useEffect(() => {
    setData(null);
    setError("");
  }, [path]);
  useEffect(() => {
    setLoading(path !== null);
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
      } finally {
        if (active) setLoading(false);
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
  return { data, error, refresh, loading };
}
