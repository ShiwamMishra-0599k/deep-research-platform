import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AxiosAdapter, InternalAxiosRequestConfig } from "axios";
import { AxiosError } from "axios";
import { api, apiError, authHeaders, endSession, onSessionEnded, readCsrfToken, refreshSession } from "@/lib/api";
import { stubLocation } from "@/test/location";

// The axios adapter is the network boundary: replacing it keeps the REAL interceptors under test.
type Reply = { status: number; data?: unknown };
let seen: InternalAxiosRequestConfig[] = [];
let route: (config: InternalAxiosRequestConfig, n: number) => Reply | Promise<Reply>;

function useAdapter(handler: typeof route) {
  route = handler;
  const adapter: AxiosAdapter = async (config) => {
    seen.push(config);
    const r = await route(config, seen.length);
    const res = { data: r.data ?? {}, status: r.status, statusText: "", headers: {}, config };
    if (r.status >= 400) throw new AxiosError("fail", String(r.status), config, null, res as never);
    return res as never;
  };
  api.defaults.adapter = adapter;
}
const urls = () => seen.map((c) => `${(c.method || "get").toUpperCase()} ${c.url}`);
const setCookie = (c: string) => { document.cookie = c; };
const clearCookies = () => {
  for (const name of ["lumen_csrf", "__Host-lumen_csrf", "xlumen_csrf", "other"]) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
};

beforeEach(() => { seen = []; clearCookies(); });
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); vi.unstubAllEnvs(); });

describe("CSRF cookie", () => {
  it("is absent when no cookie exists", () => {
    expect(readCsrfToken()).toBeNull();
  });
  it("reads the development cookie name", () => {
    setCookie("lumen_csrf=abc.def");
    expect(readCsrfToken()).toBe("abc.def");
  });
  it("prefers the production __Host- name when both exist", () => {
    // jsdom (http) refuses to store __Host- cookies, so present the browser's cookie string directly.
    const spy = vi.spyOn(document, "cookie", "get").mockReturnValue("lumen_csrf=dev; __Host-lumen_csrf=prod");
    expect(readCsrfToken()).toBe("prod");
    spy.mockRestore();
  });
  it("does not confuse a cookie whose name merely ends with the same text", () => {
    setCookie("xlumen_csrf=wrong");
    expect(readCsrfToken()).toBeNull();
  });
  it("decodes a URI-encoded value and ignores unrelated cookies", () => {
    setCookie("other=1");
    setCookie("lumen_csrf=a%3Db.c");
    expect(readCsrfToken()).toBe("a=b.c");
  });
});

describe("authHeaders", () => {
  it("always announces cookie mode and adds the CSRF token only to state-changing methods", () => {
    setCookie("lumen_csrf=tok");
    for (const m of ["post", "PUT", "Patch", "DELETE"]) expect(authHeaders(m)).toEqual({ "X-Auth-Mode": "cookie", "X-CSRF-Token": "tok" });
    for (const m of ["get", "HEAD", "OPTIONS"]) expect(authHeaders(m)).toEqual({ "X-Auth-Mode": "cookie" });
  });
  it("omits the CSRF header rather than sending an empty one when there is no cookie", () => {
    expect(authHeaders("POST")).toEqual({ "X-Auth-Mode": "cookie" });
  });
});

describe("request interceptor", () => {
  it("sends X-Auth-Mode: cookie on every request", async () => {
    useAdapter(() => ({ status: 200 }));
    await api.get("/conversations");
    expect(seen[0].headers.get("X-Auth-Mode")).toBe("cookie");
  });

  it("NEVER sends Authorization, even if a legacy token is still in web storage", async () => {
    localStorage.setItem("auth_token", "legacy-token");
    useAdapter(() => ({ status: 200 }));
    await api.get("/conversations");
    await api.post("/conversations", {});
    for (const c of seen) expect(c.headers.get("Authorization")).toBeFalsy();
  });

  it("adds the CSRF token on POST/PATCH/DELETE and not on GET", async () => {
    setCookie("lumen_csrf=csrf-1");
    useAdapter(() => ({ status: 200 }));
    await api.get("/a");
    await api.post("/b", {});
    await api.patch("/c", {});
    await api.delete("/d");
    expect(seen.map((c) => c.headers.get("X-CSRF-Token") ?? null)).toEqual([null, "csrf-1", "csrf-1", "csrf-1"]);
  });

  it("uses same-origin relative /api without credentials opt-in", async () => {
    useAdapter(() => ({ status: 200 }));
    await api.get("/x");
    expect(seen[0].baseURL).toBe("/api");
    expect(seen[0].withCredentials).toBe(false);
  });

  it.each([
    ["a different origin", "https://api.other-site.example/api", true],
    ["the same absolute origin", "http://localhost:3000/api", false],
  ])("withCredentials is %s => %s", async (_label, url, expected) => {
    vi.stubEnv("VITE_API_URL", url);
    vi.resetModules();
    const fresh = await import("@/lib/api");
    expect(fresh.api.defaults.withCredentials).toBe(expected);
    expect(fresh.crossOrigin).toBe(expected);
  });
});

describe("response interceptor: refresh and replay on 401", () => {
  it("refreshes once, replays the original request once, and returns its result to the caller", async () => {
    let first = true;
    useAdapter((c) => {
      if (c.url === "/auth/refresh") return { status: 200 };
      if (first) { first = false; return { status: 401 }; }
      return { status: 200, data: { ok: "replayed" } };
    });
    const r = await api.get("/conversations");
    expect(r.data).toEqual({ ok: "replayed" });
    expect(urls()).toEqual(["GET /conversations", "POST /auth/refresh", "GET /conversations"]);
  });

  it("the replay carries the freshly rotated CSRF token, not the stale one", async () => {
    setCookie("lumen_csrf=old");
    let first = true;
    useAdapter((c) => {
      if (c.url === "/auth/refresh") { setCookie("lumen_csrf=new"); return { status: 200 }; }
      if (first) { first = false; return { status: 401 }; }
      return { status: 200 };
    });
    await api.post("/conversations", {});
    expect(seen[0].headers.get("X-CSRF-Token")).toBe("old");
    expect(seen[1].headers.get("X-CSRF-Token")).toBe("old"); // the refresh itself is a POST sent before rotation
    expect(seen[2].headers.get("X-CSRF-Token")).toBe("new");
  });

  it("SINGLE-FLIGHT: N concurrent 401s share exactly one refresh request and each is replayed once", async () => {
    const seenPaths = new Map<string, number>();
    let refreshes = 0;
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    useAdapter(async (c) => {
      if (c.url === "/auth/refresh") { refreshes++; await gate; return { status: 200 }; }
      const n = (seenPaths.get(c.url!) ?? 0) + 1;
      seenPaths.set(c.url!, n);
      return n === 1 ? { status: 401 } : { status: 200, data: c.url };
    });
    const calls = Promise.all([1, 2, 3, 4, 5].map((i) => api.get(`/r${i}`)));
    await vi.waitFor(() => expect(refreshes).toBe(1)); // all five have hit 401 and are waiting on the one refresh
    release();
    const results = await calls;
    expect(results.map((r) => r.data)).toEqual(["/r1", "/r2", "/r3", "/r4", "/r5"]);
    expect(refreshes).toBe(1);
    expect(seen.filter((c) => c.url !== "/auth/refresh")).toHaveLength(10); // 5 originals + 5 replays
  });

  it("a later 401 starts a NEW refresh (the shared promise is cleared after it settles)", async () => {
    let refreshes = 0;
    const hits = new Map<string, number>();
    useAdapter((c) => {
      if (c.url === "/auth/refresh") { refreshes++; return { status: 200 }; }
      const n = (hits.get(c.url!) ?? 0) + 1;
      hits.set(c.url!, n);
      return n === 1 ? { status: 401 } : { status: 200 };
    });
    await api.get("/one");
    await api.get("/two");
    expect(refreshes).toBe(2);
  });

  it("NEVER LOOPS: if the replay is rejected too, the session is ended and refresh was attempted only once", async () => {
    const loc = stubLocation("/c/abc");
    const ended = vi.fn();
    const off = onSessionEnded(ended);
    useAdapter((c) => (c.url === "/auth/refresh" ? { status: 200 } : { status: 401 }));
    await expect(api.get("/conversations")).rejects.toMatchObject({ response: { status: 401 } });
    expect(urls()).toEqual(["GET /conversations", "POST /auth/refresh", "GET /conversations"]);
    expect(ended).toHaveBeenCalledTimes(1);
    expect(loc.assigned).toEqual(["/login"]);
    off();
  });

  it.each([401, 403])("refresh answered %i (session over): purge, notify, redirect to /login, original error re-thrown", async (status) => {
    const loc = stubLocation("/c/abc");
    localStorage.setItem("auth_token", "old");
    localStorage.setItem("auth_user", '{"id":"1"}');
    const ended = vi.fn();
    const off = onSessionEnded(ended);
    useAdapter((c) => (c.url === "/auth/refresh" ? { status } : { status: 401 }));
    await expect(api.get("/conversations")).rejects.toMatchObject({ response: { status: 401 } });
    expect(urls()).toEqual(["GET /conversations", "POST /auth/refresh"]); // original is NOT replayed
    expect(localStorage.getItem("auth_token")).toBeNull();
    expect(localStorage.getItem("auth_user")).toBeNull();
    expect(ended).toHaveBeenCalledTimes(1);
    expect(loc.assigned).toEqual(["/login"]);
    off();
  });

  it.each([429, 500, 503])("a TRANSIENT refresh failure (%i) does not sign the user out: no purge, no redirect", async (status) => {
    const loc = stubLocation("/");
    const ended = vi.fn();
    const off = onSessionEnded(ended);
    useAdapter((c) => (c.url === "/auth/refresh" ? { status } : { status: 401 }));
    await expect(api.get("/x")).rejects.toMatchObject({ response: { status: 401 } });
    expect(ended).not.toHaveBeenCalled();
    expect(loc.assigned).toEqual([]);
    off();
  });

  it("a network error during refresh is transient too", async () => {
    const loc = stubLocation("/");
    useAdapter((c) => {
      if (c.url === "/auth/refresh") throw new AxiosError("Network Error", "ERR_NETWORK", c);
      return { status: 401 };
    });
    await expect(api.get("/x")).rejects.toMatchObject({ response: { status: 401 } });
    expect(loc.assigned).toEqual([]);
  });

  it("409 'Refresh in progress': waits about a second, retries refresh ONCE, then replays; session untouched", async () => {
    vi.useFakeTimers();
    const loc = stubLocation("/");
    let refreshes = 0;
    let first = true;
    useAdapter((c) => {
      if (c.url === "/auth/refresh") { refreshes++; return refreshes === 1 ? { status: 409, data: { detail: "Refresh in progress" } } : { status: 200 }; }
      if (first) { first = false; return { status: 401 }; }
      return { status: 200, data: "ok" };
    });
    const p = api.get("/x");
    await vi.advanceTimersByTimeAsync(999);
    expect(refreshes).toBe(1); // still waiting: no hammering
    await vi.advanceTimersByTimeAsync(2);
    const r = await p;
    expect(r.data).toBe("ok");
    expect(refreshes).toBe(2);
    expect(loc.assigned).toEqual([]);
  });

  it("a second 409 is not retried again and does NOT clear the session", async () => {
    vi.useFakeTimers();
    const loc = stubLocation("/");
    const ended = vi.fn();
    const off = onSessionEnded(ended);
    let refreshes = 0;
    useAdapter((c) => {
      if (c.url === "/auth/refresh") { refreshes++; return { status: 409 }; }
      return { status: 401 };
    });
    const p = api.get("/x").catch((e) => e);
    await vi.advanceTimersByTimeAsync(1500);
    expect((await p).response.status).toBe(401);
    expect(refreshes).toBe(2);
    expect(ended).not.toHaveBeenCalled();
    expect(loc.assigned).toEqual([]);
    off();
  });

  it.each(["/auth/login", "/auth/register", "/auth/me", "/auth/logout", "/auth/session/upgrade", "/auth/refresh"])(
    "401 from %s is the handshake itself: no refresh attempt, no purge, no redirect",
    async (path) => {
      const loc = stubLocation("/");
      const ended = vi.fn();
      const off = onSessionEnded(ended);
      useAdapter(() => ({ status: 401 }));
      await expect(api.post(path, {})).rejects.toMatchObject({ response: { status: 401 } });
      expect(urls()).toEqual([`POST ${path}`]);
      expect(ended).not.toHaveBeenCalled();
      expect(loc.assigned).toEqual([]);
      off();
    },
  );

  it.each([403, 404, 429, 500])("a %i is passed through untouched (no refresh, no sign-out)", async (status) => {
    const loc = stubLocation("/");
    useAdapter(() => ({ status }));
    await expect(api.get("/x")).rejects.toMatchObject({ response: { status } });
    expect(urls()).toEqual(["GET /x"]);
    expect(loc.assigned).toEqual([]);
  });

  it("passes successful responses through untouched", async () => {
    useAdapter(() => ({ status: 200, data: { ok: true } }));
    expect((await api.get("/x")).data).toEqual({ ok: true });
  });

  it("an error without a request config is re-thrown as is", async () => {
    // exercises the guard `!cfg`: nothing to replay
    const handler = (api.interceptors.response as any).handlers.find((h: any) => h?.rejected)?.rejected as (e: unknown) => Promise<never>;
    const err = { response: { status: 401 } };
    await expect(handler(err)).rejects.toBe(err);
  });
});

describe("endSession / onSessionEnded", () => {
  it("does not redirect when already on /login or /register, but still notifies and purges legacy keys", () => {
    for (const path of ["/login", "/register"]) {
      const loc = stubLocation(path);
      const fn = vi.fn();
      const off = onSessionEnded(fn);
      localStorage.setItem("auth_token", "x");
      endSession();
      expect(fn).toHaveBeenCalledTimes(1);
      expect(loc.assigned).toEqual([]);
      expect(localStorage.getItem("auth_token")).toBeNull();
      off();
    }
  });

  it("unsubscribing stops notifications", () => {
    stubLocation("/login");
    const fn = vi.fn();
    const off = onSessionEnded(fn);
    off();
    endSession();
    expect(fn).not.toHaveBeenCalled();
  });

  it("refreshSession() called directly is also single-flight", async () => {
    let n = 0;
    useAdapter((c) => { if (c.url === "/auth/refresh") n++; return { status: 200 }; });
    const [a, b] = await Promise.all([refreshSession(), refreshSession()]);
    expect([a, b]).toEqual(["ok", "ok"]);
    expect(n).toBe(1);
  });
});

describe("apiError", () => {
  it("returns a string detail verbatim", () => {
    expect(apiError({ response: { data: { detail: "Invalid credentials" } } }, "fb")).toBe("Invalid credentials");
  });
  it("returns the first validation message from a list detail", () => {
    expect(apiError({ response: { data: { detail: [{ msg: "Password too short" }, { msg: "x" }] } } }, "fb")).toBe("Password too short");
  });
  it.each([
    ["no response (network error)", {}],
    ["empty body", { response: { data: {} } }],
    ["empty list", { response: { data: { detail: [] } } }],
    ["list without msg", { response: { data: { detail: [{}] } } }],
    ["non-string non-list detail", { response: { data: { detail: 42 } } }],
    ["null error", null],
  ])("falls back when %s", (_label, err) => {
    expect(apiError(err, "Something failed")).toBe("Something failed");
  });
});
