/** Typed client for the v1 API (§13.1): one error envelope, cursor paging, ETag writes, tickets. */

const API_BASE = "/api/v1";

export class ApiFailure extends Error {
  readonly code: string;
  readonly status: number;
  readonly requestId?: string;
  readonly details?: Record<string, unknown>;

  constructor(status: number, payload: any) {
    const error = payload?.error ?? {};
    super(error.message ?? `Request failed with status ${status}`);
    this.status = status;
    this.code = error.code ?? "INTERNAL";
    this.requestId = error.request_id;
    this.details = error.details;
  }
}

/** Dev mode reads the token from storage; an OIDC deployment would exchange a code for one here. */
export function token(): string | null {
  return localStorage.getItem("aita.token");
}

export function setToken(value: string | null): void {
  if (value) localStorage.setItem("aita.token", value);
  else localStorage.removeItem("aita.token");
}

export function tenantHint(): string | null {
  return localStorage.getItem("aita.tenant");
}

export function setTenantHint(value: string | null): void {
  if (value) localStorage.setItem("aita.tenant", value);
  else localStorage.removeItem("aita.tenant");
}

type Options = {
  method?: string;
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined | null>;
  headers?: Record<string, string>;
  raw?: boolean;
};

function searchParams(query?: Options["query"]): string {
  if (!query) return "";
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue;
    search.append(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

async function request<T = any>(path: string, options: Options = {}): Promise<T> {
  const bearer = token();
  const tenant = tenantHint();
  const headers: Record<string, string> = { ...options.headers };
  if (bearer) headers["Authorization"] = `Bearer ${bearer}`;
  if (tenant) headers["X-Tenant-Id"] = tenant;
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(`${API_BASE}${path}${searchParams(options.query)}`, {
    method: options.method ?? "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const requestId = response.headers.get("X-Request-ID") ?? undefined;
  if (options.raw) {
    if (!response.ok) throw new ApiFailure(response.status, { error: { message: await response.text(), request_id: requestId } });
    return (await response.blob()) as unknown as T;
  }
  if (response.status === 204) return null as T;
  const payload = await response.json().catch(() => ({ error: { message: "The response was not JSON", request_id: requestId } }));
  if (!response.ok) throw new ApiFailure(response.status, payload);
  return payload as T;
}

export const api = {
  get: <T = any>(path: string, query?: Options["query"]) => request<T>(path, { query }),
  post: <T = any>(path: string, body?: unknown, headers?: Record<string, string>) =>
    request<T>(path, { method: "POST", body: body ?? {}, headers }),
  put: <T = any>(path: string, body?: unknown, headers?: Record<string, string>) =>
    request<T>(path, { method: "PUT", body: body ?? {}, headers }),
  patch: <T = any>(path: string, body?: unknown, headers?: Record<string, string>) =>
    request<T>(path, { method: "PATCH", body: body ?? {}, headers }),
  del: <T = any>(path: string) => request<T>(path, { method: "DELETE" }),
  blob: (path: string, query?: Options["query"]) => request<Blob>(path, { query, raw: true }),
  etag: (path: string, query?: Options["query"]) =>
    fetch(`${API_BASE}${path}${searchParams(query)}`, {
      headers: authHeaders(),
    }).then(async (response) => {
      if (!response.ok) throw new ApiFailure(response.status, await response.json().catch(() => ({})));
      return { body: await response.json(), etag: response.headers.get("ETag") };
    }),
  /** Multipart upload: attachments are bytes with a scan verdict, never a JSON body (§6.4). */
  form: <T = any>(path: string, payload: FormData, method = "POST") =>
    fetch(`${API_BASE}${path}`, { method, headers: authHeaders(), body: payload }).then(async (response) => {
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new ApiFailure(response.status, body);
      return body as T;
    }),
};

/** Tickets are single-use and consumed by the server at connect time, so the URL is built once (§14.3). */
export function absoluteUrl(path: string): string {
  if (path.startsWith("http")) return path;
  return path.startsWith(API_BASE) ? path : `${API_BASE}${path}`;
}

/** Server-issued URLs carry the API prefix; strip it before a call that re-adds it. */
export function localPath(url: string): string {
  return url.startsWith(API_BASE) ? url.slice(API_BASE.length) : url;
}

export function socketUrl(path: string): string {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}${API_BASE}${localPath(path)}`;
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  const bearer = token();
  const tenant = tenantHint();
  if (bearer) headers["Authorization"] = `Bearer ${bearer}`;
  if (tenant) headers["X-Tenant-Id"] = tenant;
  return headers;
}

/** Repeatable filters are arrays in the query string; empty entries are dropped server-side too. */
export function listQuery(page: { limit: number; cursor?: string | null }, filters: Record<string, string | string[] | undefined>) {
  const query: Record<string, string> = { limit: String(page.limit) };
  if (page.cursor) query.cursor = page.cursor;
  for (const [key, value] of Object.entries(filters)) {
    if (value === undefined || (Array.isArray(value) && value.length === 0)) continue;
    query[key] = Array.isArray(value) ? value.join(",") : value;
  }
  return query;
}

export type Page<T> = { items: T[]; next_cursor?: string | null; total?: number };
