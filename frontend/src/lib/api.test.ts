import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AxiosAdapter, InternalAxiosRequestConfig } from "axios";
import { AxiosError } from "axios";
import { api, apiError } from "@/lib/api";
import { stubLocation } from "@/test/location";

// The axios adapter is the network boundary: replacing it keeps the real interceptors under test.
let seen: InternalAxiosRequestConfig[] = [];
function respondWith(status: number, data: unknown = {}) {
  const adapter: AxiosAdapter = async (config) => {
    seen.push(config);
    const res = { data, status, statusText: "", headers: {}, config };
    if (status >= 400) throw new AxiosError("fail", String(status), config, null, res as never);
    return res as never;
  };
  api.defaults.adapter = adapter;
}

beforeEach(() => { seen = []; });
afterEach(() => { vi.unstubAllGlobals(); });

describe("request interceptor", () => {
  it("sends the stored token as a Bearer header", async () => {
    localStorage.setItem("auth_token", "tok-123");
    respondWith(200);
    await api.get("/conversations");
    expect(seen[0].headers.Authorization).toBe("Bearer tok-123");
  });

  it("sends no Authorization header when signed out", async () => {
    respondWith(200);
    await api.get("/conversations");
    expect(seen[0].headers.Authorization).toBeUndefined();
  });

  it("uses /api as the default base URL", async () => {
    respondWith(200);
    await api.get("/x");
    expect(seen[0].baseURL).toBe("/api");
  });
});

describe("response interceptor: expired or invalid session (401)", () => {
  it("purges stored credentials and redirects to /login", async () => {
    const loc = stubLocation("/c/abc");
    localStorage.setItem("auth_token", "old");
    localStorage.setItem("auth_user", '{"id":"1"}');
    respondWith(401);
    await expect(api.get("/conversations")).rejects.toBeInstanceOf(AxiosError);
    expect(localStorage.getItem("auth_token")).toBeNull();
    expect(localStorage.getItem("auth_user")).toBeNull();
    expect(loc.assigned).toEqual(["/login"]);
  });

  it.each(["/login", "/register"])("does not redirect when already on %s (no loop) but still purges", async (path) => {
    const loc = stubLocation(path);
    localStorage.setItem("auth_token", "old");
    respondWith(401);
    await expect(api.post("/auth/login", {})).rejects.toBeDefined();
    expect(loc.assigned).toEqual([]);
    expect(localStorage.getItem("auth_token")).toBeNull();
  });

  it.each([403, 404, 429, 500])("leaves the session alone on %i and re-throws the error", async (status) => {
    const loc = stubLocation("/");
    localStorage.setItem("auth_token", "keep");
    respondWith(status);
    await expect(api.get("/x")).rejects.toMatchObject({ response: { status } });
    expect(localStorage.getItem("auth_token")).toBe("keep");
    expect(loc.assigned).toEqual([]);
  });

  it("passes successful responses through untouched", async () => {
    respondWith(200, { ok: true });
    const r = await api.get("/x");
    expect(r.data).toEqual({ ok: true });
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
