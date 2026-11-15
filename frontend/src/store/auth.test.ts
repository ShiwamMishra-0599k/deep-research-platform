import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation, Message } from "@/types";

const http = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn() }));
vi.mock("@/lib/api", () => ({ api: http, apiError: () => "" }));

import { useAuth } from "@/store/auth";
import { useChat } from "@/store/chat";

const alice = { id: "u1", email: "alice@example.com", name: "Alice" };
const bob = { id: "u2", email: "bob@example.com", name: "Bob" };

beforeEach(() => {
  http.post.mockReset();
  useAuth.setState({ user: null, loading: false });
  useChat.setState({ conversations: [], activeId: null, messages: {}, loading: false });
});

describe("login", () => {
  it("stores token and user, updates state, and toggles loading", async () => {
    let during = false;
    http.post.mockImplementation(async () => { during = useAuth.getState().loading; return { data: { token: "t1", ...alice } }; });
    await useAuth.getState().login("alice@example.com", "pw");
    expect(http.post).toHaveBeenCalledWith("/auth/login", { email: "alice@example.com", password: "pw" });
    expect(during).toBe(true);
    expect(useAuth.getState().loading).toBe(false);
    expect(useAuth.getState().user).toEqual(alice);
    expect(localStorage.getItem("auth_token")).toBe("t1");
    expect(JSON.parse(localStorage.getItem("auth_user")!)).toEqual(alice);
  });

  it("does NOT keep the token from the server response in the stored user", async () => {
    http.post.mockResolvedValue({ data: { token: "secret", ...alice } });
    await useAuth.getState().login("a", "b");
    expect(localStorage.getItem("auth_user")).not.toContain("secret");
    expect(useAuth.getState().user).not.toHaveProperty("token");
  });

  it("on failure: re-throws, clears loading, stores nothing, user stays signed out", async () => {
    http.post.mockRejectedValue({ response: { status: 401 } });
    await expect(useAuth.getState().login("a", "wrong")).rejects.toBeDefined();
    expect(useAuth.getState().loading).toBe(false);
    expect(useAuth.getState().user).toBeNull();
    expect(localStorage.getItem("auth_token")).toBeNull();
    expect(localStorage.getItem("auth_user")).toBeNull();
  });
});

describe("register", () => {
  it("posts name/email/password, then behaves like a login", async () => {
    http.post.mockResolvedValue({ data: { token: "t2", ...bob } });
    await useAuth.getState().register("bob@example.com", "password-1", "Bob");
    expect(http.post).toHaveBeenCalledWith("/auth/register", { email: "bob@example.com", password: "password-1", name: "Bob" });
    expect(useAuth.getState().user).toEqual(bob);
    expect(localStorage.getItem("auth_token")).toBe("t2");
  });

  it("on a duplicate email (409) stores nothing and clears loading", async () => {
    http.post.mockRejectedValue({ response: { status: 409 } });
    await expect(useAuth.getState().register("a", "b", "c")).rejects.toBeDefined();
    expect(useAuth.getState().loading).toBe(false);
    expect(localStorage.getItem("auth_token")).toBeNull();
  });
});

describe("logout", () => {
  it("removes both stored credentials and clears the user", () => {
    localStorage.setItem("auth_token", "t");
    localStorage.setItem("auth_user", JSON.stringify(alice));
    useAuth.setState({ user: alice });
    useAuth.getState().logout();
    expect(localStorage.getItem("auth_token")).toBeNull();
    expect(localStorage.getItem("auth_user")).toBeNull();
    expect(useAuth.getState().user).toBeNull();
  });

  it("REGRESSION (cross-user leak): resets the chat store so the next person on this tab sees nothing of the previous user's", () => {
    const secret: Message = { id: "m", role: "assistant", content: "Alice's private research", createdAt: "t" };
    const c: Conversation = { id: "c1", title: "Alice's chat", createdAt: "t", updatedAt: "t" };
    useAuth.setState({ user: alice });
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
    useAuth.setState({ user: alice });
    useChat.setState({ activeId: "c1", messages: { c1: [{ id: "m", role: "user", content: "x", createdAt: "t" }] } });
    useAuth.getState().logout();
    http.post.mockResolvedValue({ data: { token: "t2", ...bob } });
    await useAuth.getState().login("bob@example.com", "pw");
    expect(useAuth.getState().user).toEqual(bob);
    expect(useChat.getState().activeId).toBeNull();
    expect(useChat.getState().messages).toEqual({});
  });
});

describe("session hydration", () => {
  it("REGRESSION (reload bounced to /login): the stored user is available synchronously at store creation", async () => {
    localStorage.setItem("auth_user", JSON.stringify(alice));
    vi.resetModules();
    const fresh = await import("@/store/auth");
    expect(fresh.useAuth.getState().user).toEqual(alice); // no effect/tick needed => no login flash
  });

  it("a signed-out browser starts with no user", async () => {
    vi.resetModules();
    const fresh = await import("@/store/auth");
    expect(fresh.useAuth.getState().user).toBeNull();
  });

  it.each([["not json{"], ["undefined"], ["[1,"]])("malformed stored user %j yields null, never a crash", async (raw) => {
    localStorage.setItem("auth_user", raw);
    vi.resetModules();
    const fresh = await import("@/store/auth");
    expect(fresh.useAuth.getState().user).toBeNull();
  });

  it("hydrate() loads the stored user into state", () => {
    localStorage.setItem("auth_user", JSON.stringify(bob));
    useAuth.getState().hydrate();
    expect(useAuth.getState().user).toEqual(bob);
  });

  it("hydrate() ignores malformed JSON and keeps the current user", () => {
    useAuth.setState({ user: alice });
    localStorage.setItem("auth_user", "{bad");
    expect(() => useAuth.getState().hydrate()).not.toThrow();
    expect(useAuth.getState().user).toEqual(alice);
  });

  it("hydrate() with nothing stored leaves state alone", () => {
    useAuth.setState({ user: alice });
    useAuth.getState().hydrate();
    expect(useAuth.getState().user).toEqual(alice);
  });
});
