import axios from "axios";

declare module "axios" {
  interface InternalAxiosRequestConfig {
    /** Set once a request has been replayed after a successful refresh, so it can never loop. */
    _retried?: boolean;
  }
}

export const baseURL: string = import.meta.env.VITE_API_URL || "/api";

/**
 * Same-origin topology (production, and dev through the Vite proxy): relative `/api`, browser sends cookies by default.
 * Only an API on a DIFFERENT origin needs credentials to be opted in explicitly.
 */
export const crossOrigin: boolean = (() => {
  if (!/^https?:\/\//i.test(baseURL)) return false;
  try {
    return new URL(baseURL).origin !== location.origin;
  } catch {
    return false;
  }
})();

// ── CSRF / auth-mode headers ────────────────────────────────────────────────

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);
// Production names carry a prefix (cookies set with Secure); development over http uses the plain name.
const CSRF_COOKIES = ["__Host-lumen_csrf", "lumen_csrf"];

/** The CSRF cookie is readable by script on purpose (it is useless without the HttpOnly session). */
export function readCsrfToken(): string | null {
  for (const name of CSRF_COOKIES) {
    const hit = document.cookie.split("; ").find((c) => c.startsWith(name + "="));
    if (hit) return decodeURIComponent(hit.slice(name.length + 1));
  }
  return null;
}

/** Headers every request carries. `X-Auth-Mode: cookie` tells the server not to return a legacy body token. */
export function authHeaders(method: string): Record<string, string> {
  const h: Record<string, string> = { "X-Auth-Mode": "cookie" };
  if (UNSAFE.has(method.toUpperCase())) {
    const csrf = readCsrfToken();
    if (csrf) h["X-CSRF-Token"] = csrf;
  }
  return h;
}

// ── session-ended notification (the auth store subscribes; api.ts must not import it) ──

const endedListeners = new Set<() => void>();
export function onSessionEnded(fn: () => void): () => void {
  endedListeners.add(fn);
  return () => endedListeners.delete(fn);
}

/** The server says the session is over: forget everything locally and go to sign-in (unless already there). */
export function endSession(): void {
  try {
    // Pre-migration keys; harmless to remove again.
    localStorage.removeItem("auth_token");
    localStorage.removeItem("auth_user");
  } catch { /* storage unavailable */ }
  endedListeners.forEach((fn) => fn());
  if (!location.pathname.startsWith("/login") && !location.pathname.startsWith("/register")) {
    location.href = "/login";
  }
}

// ── axios instance ──────────────────────────────────────────────────────────

export const api = axios.create({ baseURL, withCredentials: crossOrigin });

api.interceptors.request.use((config) => {
  for (const [k, v] of Object.entries(authHeaders(config.method || "get"))) config.headers.set(k, v);
  return config;
});

// Requests that are part of the auth handshake itself never trigger a refresh (that would loop or hide real errors).
const NO_REFRESH = ["/auth/login", "/auth/register", "/auth/refresh", "/auth/logout", "/auth/session/upgrade", "/auth/me"];

export type RefreshResult = "ok" | "ended" | "failed";

let inflight: Promise<RefreshResult> | null = null;
const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

async function doRefresh(): Promise<RefreshResult> {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      await api.post("/auth/refresh");
      return "ok";
    } catch (e: any) {
      const status = e?.response?.status;
      // 409: another tab just rotated the token; the browser already holds the new cookie. Retry once.
      if (status === 409 && attempt === 0) {
        await sleep(1000);
        continue;
      }
      if (status === 401 || status === 403) return "ended"; // the session is over (or CSRF is unrecoverable)
      return "failed"; // network / 429 / 5xx: transient, do not sign the user out
    }
  }
  return "failed";
}

/** Single-flight: any number of concurrent 401s share ONE refresh request. */
export function refreshSession(): Promise<RefreshResult> {
  if (!inflight) inflight = doRefresh().finally(() => { inflight = null; });
  return inflight;
}

api.interceptors.response.use(
  (r) => r,
  async (err) => {
    const cfg = err?.config;
    if (err?.response?.status !== 401 || !cfg) return Promise.reject(err);
    if (NO_REFRESH.some((p) => String(cfg.url || "").startsWith(p))) return Promise.reject(err);

    if (cfg._retried) {            // the replay was rejected too: give up for good
      endSession();
      return Promise.reject(err);
    }
    const result = await refreshSession();
    if (result === "ok") {
      cfg._retried = true;
      return api(cfg);              // replay once, with the freshly rotated cookies and CSRF token
    }
    if (result === "ended") endSession();
    return Promise.reject(err);
  },
);

/** Best-effort message from a FastAPI error body (`detail` is a string, or a list for validation errors). */
export function apiError(e: any, fallback: string): string {
  const detail = e?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail[0]?.msg) return String(detail[0].msg);
  return fallback;
}
