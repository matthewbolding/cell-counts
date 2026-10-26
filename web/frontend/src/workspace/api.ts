// api.ts — every request to the Django API goes through here: session cookie
// plus X-CSRFToken (read from the page's <meta name="csrf-token">), JSON in and
// out, and one error type carrying the server's {error, detail} body.

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly body: Record<string, unknown>;

  constructor(status: number, code: string, detail: string, body: Record<string, unknown> = {}) {
    super(detail || code);
    this.status = status;
    this.code = code;
    this.body = body;
  }

  /** No response at all (offline, DNS, the tunnel down) — worth retrying. */
  get isNetwork(): boolean {
    return this.status === 0;
  }

  get isRetryable(): boolean {
    return this.status === 0 || this.status === 502 || this.status === 503 || this.status === 504;
  }
}

let csrfToken: string | null = null;

function csrf(): string {
  if (csrfToken === null) {
    csrfToken = document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content ?? "";
  }
  return csrfToken;
}

export interface RequestOptions {
  keepalive?: boolean;
  signal?: AbortSignal;
  headers?: Record<string, string>;
  /** Send this body as-is instead of JSON-encoding `body`. */
  raw?: BodyInit;
}

export async function request<T>(
  method: string,
  url: string,
  body?: unknown,
  options: RequestOptions = {},
): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...options.headers };
  if (method !== "GET" && method !== "HEAD") headers["X-CSRFToken"] = csrf();
  let payload: BodyInit | undefined = options.raw;
  if (payload === undefined && body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }

  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body: payload,
      credentials: "same-origin",
      keepalive: options.keepalive,
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, "network", "Could not reach the server.");
  }

  if (response.status === 204) return undefined as T;
  let data: Record<string, unknown> = {};
  const text = await response.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = {};
    }
  }
  if (!response.ok) {
    if (response.status === 401) {
      // Session expired or signed out elsewhere: back to the sign-in page.
      window.location.assign(`/accounts/login/?next=${encodeURIComponent(location.pathname)}`);
    }
    const code = typeof data.error === "string" ? data.error : `http_${response.status}`;
    const detail = typeof data.detail === "string" && data.detail ? data.detail : response.statusText;
    throw new ApiError(response.status, code, detail, data);
  }
  return data as T;
}

export const get = <T>(url: string, options?: RequestOptions) => request<T>("GET", url, undefined, options);
export const post = <T>(url: string, body?: unknown, options?: RequestOptions) =>
  request<T>("POST", url, body ?? {}, options);
export const put = <T>(url: string, body?: unknown, options?: RequestOptions) =>
  request<T>("PUT", url, body, options);
export const del = <T>(url: string, options?: RequestOptions) => request<T>("DELETE", url, undefined, options);

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}

export const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
