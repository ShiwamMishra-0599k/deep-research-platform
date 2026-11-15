import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { stubLocation } from "@/test/location";

type Opts = {
  method: string;
  headers: Record<string, string>;
  body: string;
  signal?: AbortSignal;
  openWhenHidden: boolean;
  onopen: (res: { ok: boolean; status: number }) => Promise<void>;
  onmessage: (m: unknown) => void;
  onerror: (e: unknown) => void;
  onclose: () => void;
};

const fes = vi.hoisted(() => ({ calls: [] as { url: string; opts: Opts }[], script: null as null | ((o: Opts) => Promise<void>) }));
vi.mock("@microsoft/fetch-event-source", () => ({
  fetchEventSource: async (url: string, opts: Opts) => {
    fes.calls.push({ url, opts });
    if (fes.script) await fes.script(opts);
  },
}));

import { postSse, SseHttpError } from "@/lib/sse";

beforeEach(() => { fes.calls = []; fes.script = null; });
afterEach(() => { vi.unstubAllGlobals(); });

const noop = { onEvent: () => {} };

describe("request", () => {
  it("POSTs JSON to /api + path with an event-stream Accept header", async () => {
    await postSse("/conversations/1/query", { query: "hi" }, noop);
    const { url, opts } = fes.calls[0];
    expect(url).toBe("/api/conversations/1/query");
    expect(opts.method).toBe("POST");
    expect(opts.headers["Content-Type"]).toBe("application/json");
    expect(opts.headers.Accept).toBe("text/event-stream");
    expect(JSON.parse(opts.body)).toEqual({ query: "hi" });
    expect(opts.openWhenHidden).toBe(true);
  });

  it("adds Authorization only when a token is stored", async () => {
    await postSse("/x", {}, noop);
    expect(fes.calls[0].opts.headers.Authorization).toBeUndefined();
    localStorage.setItem("auth_token", "tok-9");
    await postSse("/x", {}, noop);
    expect(fes.calls[1].opts.headers.Authorization).toBe("Bearer tok-9");
  });

  it("passes the AbortSignal through so the caller can cancel the stream", async () => {
    const ctrl = new AbortController();
    await postSse("/x", {}, { ...noop, signal: ctrl.signal });
    expect(fes.calls[0].opts.signal).toBe(ctrl.signal);
  });
});

describe("onopen", () => {
  it("accepts a 2xx response without throwing", async () => {
    fes.script = async (o) => { await o.onopen({ ok: true, status: 200 }); };
    await expect(postSse("/x", {}, noop)).resolves.toBeUndefined();
  });

  it.each([404, 413, 422, 429, 500, 503])("throws SseHttpError(%i) for a non-2xx response and keeps the session", async (status) => {
    const loc = stubLocation("/");
    localStorage.setItem("auth_token", "keep");
    fes.script = async (o) => { await o.onopen({ ok: false, status }); };
    const err = await postSse("/x", {}, noop).catch((e) => e);
    expect(err).toBeInstanceOf(SseHttpError);
    expect(err.status).toBe(status);
    expect(localStorage.getItem("auth_token")).toBe("keep");
    expect(loc.assigned).toEqual([]);
  });

  it("401: purges credentials, redirects to /login and still throws SseHttpError(401)", async () => {
    const loc = stubLocation("/c/abc");
    localStorage.setItem("auth_token", "old");
    localStorage.setItem("auth_user", "{}");
    fes.script = async (o) => { await o.onopen({ ok: false, status: 401 }); };
    const err = await postSse("/x", {}, noop).catch((e) => e);
    expect(err).toBeInstanceOf(SseHttpError);
    expect(err.status).toBe(401);
    expect(localStorage.getItem("auth_token")).toBeNull();
    expect(localStorage.getItem("auth_user")).toBeNull();
    expect(loc.assigned).toEqual(["/login"]);
  });

  it.each(["/login", "/register"])("401 on %s does not redirect again", async (path) => {
    const loc = stubLocation(path);
    fes.script = async (o) => { await o.onopen({ ok: false, status: 401 }); };
    await postSse("/x", {}, noop).catch(() => {});
    expect(loc.assigned).toEqual([]);
  });
});

describe("stream callbacks", () => {
  it("forwards every message to onEvent, in order", async () => {
    const got: unknown[] = [];
    fes.script = async (o) => {
      o.onmessage({ event: "checkpoint", data: "1" });
      o.onmessage({ event: "blocks", data: "2" });
    };
    await postSse("/x", {}, { onEvent: (m) => got.push(m) });
    expect(got).toEqual([{ event: "checkpoint", data: "1" }, { event: "blocks", data: "2" }]);
  });

  it("reports a transport error to onError and re-throws so the library stops retrying", async () => {
    const onError = vi.fn();
    const boom = new Error("network down");
    let thrown: unknown;
    fes.script = async (o) => { try { o.onerror(boom); } catch (e) { thrown = e; } };
    await postSse("/x", {}, { ...noop, onError });
    expect(onError).toHaveBeenCalledWith(boom);
    expect(thrown).toBe(boom);
  });

  it("calls onClose when the stream ends", async () => {
    const onClose = vi.fn();
    fes.script = async (o) => o.onclose();
    await postSse("/x", {}, { ...noop, onClose });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("tolerates missing optional handlers", async () => {
    fes.script = async (o) => { o.onclose(); try { o.onerror(new Error("x")); } catch { /* expected */ } };
    await expect(postSse("/x", {}, noop)).resolves.toBeUndefined();
  });
});

describe("SseHttpError", () => {
  it("carries the status and a readable message", () => {
    const e = new SseHttpError(429);
    expect(e.status).toBe(429);
    expect(e.message).toBe("HTTP 429");
    expect(e).toBeInstanceOf(Error);
  });
});
