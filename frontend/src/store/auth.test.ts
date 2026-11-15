import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation, Message } from "@/types";

const http = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn() }));
const ended = vi.hoisted(() => ({ fns: [] as (() => void)[] }));
vi.mock("@/lib/api", () => ({
  api: http,
  apiError: () => "",
  onSessionEnded: (fn: () => void) => { ended.fns.push(fn); return () => {}; },
}));

import { useAuth } from "@/store/auth";
import { useChat } from "@/store/chat";

const alice = { id: "u1", email: "alice@example.com", name: "Alice" };
const bob = { id: "u2", email: "bob@example.com", name: "Bob" };

function reset(over: Record<string, unknown> = {}) {
  useAuth.setState({ user: null, status: "loading", loading: false, booted: false, ...over });
}

beforeEach(() => {
  http.post.mockReset();
  http.get.mockReset();
  http.post.mockResolvedValue({ data: {} });
  reset();
  useChat.setState({ conversations: [], activeId: null, messages: {}, loading: false });
});

describe("login", () => {
  it("authenticates in memory, toggles loading, and stores NOTHING in web storage", async () => {
    let during = false;
    http.post.mockImplementation(async () => { during = useAuth.getState().loading; return { data: { token: "t1", ...alice } }; });
    await useAuth.getState().login("alice@example.com", "pw");
    expect(http.post).toHaveBeenCalledWith("/auth/login", { email: "alice@example.com", password: "pw" });
    expect(during).toBe(true);
    expect(useAuth.getState().loading).toBe(false);
    expect(useAuth.getState().user).toEqual(alice);
    expect(useAuth.getState().status).toBe("authenticated");
    expect(localStorage.length).toBe(0);
  });

  it("never keeps a token from the response body (legacy bearer/dual mode)", async () => {
    http.post.mockResolvedValue({ data: { token: "secret", ...alice } });
    await useAuth.getState().login("a", "b");
    expect(JSON.stringify(useAuth.getState())).not.toContain("secret");
    expect(useAuth.getState().user).not.toHaveProperty("token");
  });

  it("on failure: re-throws, clears loading, user stays signed out", async () => {
    reset({ status: "anonymous" });
    http.post.mockRejectedValue({ response: { status: 401 } });
    await expect(useAuth.getState().login("a", "wrong")).rejects.toBeDefined();
    expect(useAuth.getState().loading).toBe(false);
    expect(useAuth.getState().user).toBeNull();
    expect(useAuth.getState().status).toBe("anonymous");
    expect(localStorage.length).toBe(0);
  });
});

describe("register", () => {
  it("posts name/email/password, then behaves like a login", async () => {
    http.post.mockResolvedValue({ data: { token: "t2", ...bob } });
    await useAuth.getState().register("bob@example.com", "password-1", "Bob");
    expect(http.post).toHaveBeenCalledWith("/auth/register", { email: "bob@example.com", password: "password-1", name: "Bob" });
    expect(useAuth.getState().user).toEqual(bob);
    expect(useAuth.getState().status).toBe("authenticated");
    expect(localStorage.length).toBe(0);
  });

  it("on a duplicate email (409) clears loading and stays signed out", async () => {
    reset({ status: "anonymous" });
    http.post.mockRejectedValue({ response: { status: 409 } });
    await expect(useAuth.getState().register("a", "b", "c")).rejects.toBeDefined();
    expect(useAuth.getState().loading).toBe(false);
    expect(useAuth.getState().status).toBe("anonymous");
  });
});

describe("logout", () => {
  it("revokes the session server-side, clears the user and any leftover legacy keys", () => {
    localStorage.setItem("auth_token", "t");
    localStorage.setItem("auth_user", JSON.stringify(alice));
    reset({ user: alice, status: "authenticated" });
    useAuth.getState().logout();
    expect(http.post).toHaveBeenCalledWith("/auth/logout");
    expect(localStorage.getItem("auth_token")).toBeNull();
    expect(localStorage.getItem("auth_user")).toBeNull();
    expect(useAuth.getState().user).toBeNull();
    expect(useAuth.getState().status).toBe("anonymous");
  });

  it("signs out locally even when the server call fails (offline / already expired)", async () => {
    http.post.mockRejectedValue(new Error("Network Error"));
    reset({ user: alice, status: "authenticated" });
    expect(() => useAuth.getState().logout()).not.toThrow();
    await Promise.resolve();
    expect(useAuth.getState().status).toBe("anonymous");
  });

  it("REGRESSION (cross-user leak): resets the chat store so the next person on this tab sees nothing of the previous user's", () => {
    const secret: Message = { id: "m", role: "assistant", content: "Alice's private research", createdAt: "t" };
    const c: Conversation = { id: "c1", title: "Alice's chat", createdAt: "t", updatedAt: "t" };
    reset({ user: alice, status: "authenticated" });
    useChat.setState({ conversations: [c], activeId: "c1", messages: { c1: [secret] }, loading: true });

    useAuth.getState().logout();

    const chat = useChat.getState();
    expect(chat.conversations).toEqual([]);
    expect(chat.activeId).toBeNull();
    expect(chat.messages).toEqual({});
    expect(chat.loading).toBe(false);
    expect(JSON.stringify(chat)).not.toContain("Alice's private research");
  });

  it("a second user who logs in after a logout starts with a clean chat store", async () => {
    reset({ user: alice, status: "authenticated" });
    useChat.setState({ activeId: "c1", messages: { c1: [{ id: "m", role: "user", content: "x", createdAt: "t" }] } });
    useAuth.getState().logout();
    http.post.mockResolvedValue({ data: { ...bob } });
    await useAuth.getState().login("bob@example.com", "pw");
    expect(useAuth.getState().user).toEqual(bob);
    expect(useChat.getState().activeId).toBeNull();
    expect(useChat.getState().messages).toEqual({});
  });
});

describe("session ended by the API layer (refresh failed)", () => {
  it("clears user and chat and becomes anonymous", () => {
    reset({ user: alice, status: "authenticated" });
    useChat.setState({ activeId: "c1", messages: { c1: [] } });
    expect(ended.fns.length).toBeGreaterThan(0);
    ended.fns.forEach((f) => f());
    expect(useAuth.getState().user).toBeNull();
    expect(useAuth.getState().status).toBe("anonymous");
    expect(useChat.getState().activeId).toBeNull();
  });
});

describe("boot: hydrate()", () => {
  it("starts in 'loading' with no user (so protected routes wait instead of flashing the login page)", () => {
    expect(useAuth.getState().status).toBe("loading");
    expect(useAuth.getState().user).toBeNull();
  });

  it("valid cookie session: GET /auth/me authenticates", async () => {
    http.get.mockResolvedValue({ data: { ...alice } });
    await useAuth.getState().hydrate();
    expect(http.get).toHaveBeenCalledWith("/auth/me");
    expect(useAuth.getState().user).toEqual(alice);
    expect(useAuth.getState().status).toBe("authenticated");
  });

  it("no session (401): anonymous", async () => {
    http.get.mockRejectedValue({ response: { status: 401 } });
    await useAuth.getState().hydrate();
    expect(useAuth.getState().status).toBe("anonymous");
    expect(useAuth.getState().user).toBeNull();
  });

  it("runs the boot request only once (StrictMode double effects)", async () => {
    http.get.mockResolvedValue({ data: { ...alice } });
    await Promise.all([useAuth.getState().hydrate(), useAuth.getState().hydrate()]);
    expect(http.get).toHaveBeenCalledTimes(1);
  });

  it("a network error leaves the user anonymous but allows a later retry", async () => {
    http.get.mockRejectedValueOnce(new Error("Network Error"));
    await useAuth.getState().hydrate();
    expect(useAuth.getState().status).toBe("anonymous");
    http.get.mockResolvedValue({ data: { ...alice } });
    await useAuth.getState().hydrate();
    expect(useAuth.getState().status).toBe("authenticated");
  });

  it("an old-style stored user object is never trusted as identity", async () => {
    localStorage.setItem("auth_user", JSON.stringify(alice));
    http.get.mockRejectedValue({ response: { status: 401 } });
    await useAuth.getState().hydrate();
    expect(useAuth.getState().user).toBeNull();
    expect(localStorage.getItem("auth_user")).toBeNull(); // and it is purged
  });
});

describe("boot: upgrading a pre-migration session", () => {
  it("exchanges the legacy token ONCE via session/upgrade (explicit Bearer), authenticates, then purges both keys", async () => {
    localStorage.setItem("auth_token", "legacy-jwt");
    localStorage.setItem("auth_user", JSON.stringify(alice));
    http.post.mockResolvedValue({ data: { ...alice } });
    await useAuth.getState().hydrate();
    expect(http.post).toHaveBeenCalledWith("/auth/session/upgrade", undefined, { headers: { Authorization: "Bearer legacy-jwt" } });
    expect(http.get).not.toHaveBeenCalled();
    expect(useAuth.getState().status).toBe("authenticated");
    expect(useAuth.getState().user).toEqual(alice);
    expect(localStorage.getItem("auth_token")).toBeNull();
    expect(localStorage.getItem("auth_user")).toBeNull();
  });

  it("legacy token rejected (401 expired): anonymous, and the token is STILL purged", async () => {
    localStorage.setItem("auth_token", "expired");
    http.post.mockRejectedValue({ response: { status: 401 } });
    await useAuth.getState().hydrate();
    expect(useAuth.getState().status).toBe("anonymous");
    expect(http.get).not.toHaveBeenCalled();
    expect(localStorage.getItem("auth_token")).toBeNull();
  });

  it("upgrade not available (404, backend already cookie-only): anonymous, token purged", async () => {
    localStorage.setItem("auth_token", "old");
    http.post.mockRejectedValue({ response: { status: 404 } });
    await useAuth.getState().hydrate();
    expect(useAuth.getState().status).toBe("anonymous");
    expect(localStorage.getItem("auth_token")).toBeNull();
  });

  it("400 (another tab already upgraded): falls through to /auth/me and authenticates", async () => {
    localStorage.setItem("auth_token", "old");
    http.post.mockRejectedValue({ response: { status: 400 } });
    http.get.mockResolvedValue({ data: { ...alice } });
    await useAuth.getState().hydrate();
    expect(http.get).toHaveBeenCalledWith("/auth/me");
    expect(useAuth.getState().status).toBe("authenticated");
    expect(localStorage.getItem("auth_token")).toBeNull();
  });
});

describe("multi-tab logout (BroadcastChannel)", () => {
  type Channel = { onmessage: ((e: { data: unknown }) => void) | null; postMessage: ReturnType<typeof vi.fn> };
  async function freshWithChannel() {
    const channels: Channel[] = [];
    vi.stubGlobal("BroadcastChannel", class {
      onmessage: Channel["onmessage"] = null;
      postMessage = vi.fn();
      constructor(public name: string) { channels.push(this as unknown as Channel); }
    });
    vi.resetModules();
    const fresh = await import("@/store/auth");
    const chat = await import("@/store/chat");
    return { fresh, chat, channels };
  }

  it("logout broadcasts exactly one message on the 'lumen-auth' channel", async () => {
    const { fresh, channels } = await freshWithChannel();
    fresh.useAuth.setState({ user: alice, status: "authenticated" });
    fresh.useAuth.getState().logout();
    expect(channels).toHaveLength(1);
    expect(channels[0].postMessage).toHaveBeenCalledTimes(1);
    expect(channels[0].postMessage).toHaveBeenCalledWith({ type: "logout" });
    vi.unstubAllGlobals();
  });

  it("receiving a logout from another tab signs this tab out locally, with NO server call and NO rebroadcast", async () => {
    const { fresh, chat, channels } = await freshWithChannel();
    http.get.mockResolvedValue({ data: { ...alice } });
    await fresh.useAuth.getState().hydrate(); // opens the channel at boot
    chat.useChat.setState({ activeId: "c1", messages: { c1: [] } });
    http.post.mockClear();
    channels[0].onmessage!({ data: { type: "logout" } });
    expect(fresh.useAuth.getState().status).toBe("anonymous");
    expect(fresh.useAuth.getState().user).toBeNull();
    expect(chat.useChat.getState().activeId).toBeNull();
    expect(http.post).not.toHaveBeenCalled();
    expect(channels[0].postMessage).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("ignores unrelated or malformed messages", async () => {
    const { fresh, channels } = await freshWithChannel();
    http.get.mockResolvedValue({ data: { ...alice } });
    await fresh.useAuth.getState().hydrate();
    for (const data of [{ type: "other" }, null, "logout", {}]) channels[0].onmessage!({ data });
    expect(fresh.useAuth.getState().status).toBe("authenticated");
    vi.unstubAllGlobals();
  });

  it("works (without cross-tab sync) where BroadcastChannel does not exist", async () => {
    vi.stubGlobal("BroadcastChannel", undefined);
    vi.resetModules();
    const fresh = await import("@/store/auth");
    fresh.useAuth.setState({ user: alice, status: "authenticated" });
    expect(() => fresh.useAuth.getState().logout()).not.toThrow();
    expect(fresh.useAuth.getState().status).toBe("anonymous");
    vi.unstubAllGlobals();
  });
});
