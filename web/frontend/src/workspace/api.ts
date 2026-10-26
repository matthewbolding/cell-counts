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

/** PUT a blob and report bytes sent as they go. fetch() can't report upload
 * progress, and a 32 MB chunk on a slow connection takes long enough that a
 * progress bar which only moves between chunks looks stuck. */
export function putWithProgress(url: string, body: Blob, onProgress: (loaded: number) => void,
  signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new ApiError(0, "aborted", "The upload was cancelled."));
      return;
    }
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.setRequestHeader("X-CSRFToken", csrf());
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("Accept", "application/json");
    xhr.upload.addEventListener("progress", (e) => onProgress(e.loaded));
    xhr.addEventListener("error", () => reject(new ApiError(0, "network", "Could not reach the server.")));
    xhr.addEventListener("timeout", () => reject(new ApiError(0, "network", "The server took too long to answer.")));
    xhr.addEventListener("abort", () => reject(new ApiError(0, "aborted", "The upload was cancelled.")));
    signal?.addEventListener("abort", () => xhr.abort(), { once: true });
    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress(body.size);
        resolve();
        return;
      }
      let data: Record<string, unknown> = {};
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        data = {};
      }
      if (xhr.status === 401) {
        window.location.assign(`/accounts/login/?next=${encodeURIComponent(location.pathname)}`);
      }
      const code = typeof data.error === "string" ? data.error : `http_${xhr.status}`;
      const detail = typeof data.detail === "string" && data.detail ? data.detail : xhr.statusText;
      reject(new ApiError(xhr.status, code, detail, data));
    });
    xhr.send(body);
  });
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
