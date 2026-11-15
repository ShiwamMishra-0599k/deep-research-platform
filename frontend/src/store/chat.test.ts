import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation, Message } from "@/types";

const http = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), delete: vi.fn(), patch: vi.fn() }));
vi.mock("@/lib/api", () => ({ api: http, apiError: () => "" }));

import { useChat } from "@/store/chat";

const conv = (id: string, title = id): Conversation => ({ id, title, createdAt: "t", updatedAt: "t" });
const msg = (id: string, role: "user" | "assistant" = "user", content = id): Message => ({ id, role, content, createdAt: "t" });

beforeEach(() => {
  useChat.setState({ conversations: [], activeId: null, messages: {}, loading: false });
  Object.values(http).forEach((f) => f.mockReset());
});

describe("loadConversations", () => {
  it("stores the list and toggles loading around the request", async () => {
    let during = false;
    http.get.mockImplementation(async () => { during = useChat.getState().loading; return { data: [conv("a"), conv("b")] }; });
    await useChat.getState().loadConversations();
    expect(during).toBe(true);
    expect(useChat.getState().loading).toBe(false);
    expect(useChat.getState().conversations.map((c) => c.id)).toEqual(["a", "b"]);
    expect(http.get).toHaveBeenCalledWith("/conversations");
  });

  it("clears loading and re-throws when the request fails, leaving the list untouched", async () => {
    useChat.setState({ conversations: [conv("keep")] });
    http.get.mockRejectedValue(new Error("boom"));
    await expect(useChat.getState().loadConversations()).rejects.toThrow("boom");
    expect(useChat.getState().loading).toBe(false);
    expect(useChat.getState().conversations).toHaveLength(1);
  });
});

describe("selectConversation", () => {
  it("loads and caches messages, sets activeId, resolves true", async () => {
    http.get.mockResolvedValue({ data: [msg("m1"), msg("m2", "assistant")] });
    const ok = await useChat.getState().selectConversation("c1");
    expect(ok).toBe(true);
    expect(http.get).toHaveBeenCalledWith("/conversations/c1/messages");
    expect(useChat.getState().activeId).toBe("c1");
    expect(useChat.getState().messages.c1.map((m) => m.id)).toEqual(["m1", "m2"]);
  });

  it("does not hit the network again for an already cached conversation", async () => {
    useChat.setState({ messages: { c1: [msg("m1")] } });
    const ok = await useChat.getState().selectConversation("c1");
    expect(ok).toBe(true);
    expect(http.get).not.toHaveBeenCalled();
    expect(useChat.getState().activeId).toBe("c1");
  });

  it("REGRESSION (stale activeId after delete): a failed load resolves false and clears activeId", async () => {
    http.get.mockRejectedValue({ response: { status: 404 } });
    const ok = await useChat.getState().selectConversation("deleted");
    expect(ok).toBe(false);
    expect(useChat.getState().activeId).toBeNull();
    expect(useChat.getState().messages.deleted).toBeUndefined();
  });

  it("a failed load does not clear a DIFFERENT conversation the user switched to meanwhile", async () => {
    let fail!: (e: unknown) => void;
    http.get.mockReturnValue(new Promise((_, rej) => { fail = rej; }));
    const first = useChat.getState().selectConversation("slow");
    useChat.setState({ activeId: "other" }); // user clicked another conversation
    fail(new Error("late failure"));
    expect(await first).toBe(false);
    expect(useChat.getState().activeId).toBe("other");
  });
});

describe("createConversation", () => {
  it("prepends the new conversation, activates it and gives it an empty transcript", async () => {
    useChat.setState({ conversations: [conv("old")] });
    http.post.mockResolvedValue({ data: conv("new", "New chat") });
    const c = await useChat.getState().createConversation();
    expect(c.id).toBe("new");
    expect(http.post).toHaveBeenCalledWith("/conversations", {});
    const s = useChat.getState();
    expect(s.conversations.map((x) => x.id)).toEqual(["new", "old"]);
    expect(s.activeId).toBe("new");
    expect(s.messages.new).toEqual([]);
  });

  it("changes nothing when creation fails", async () => {
    http.post.mockRejectedValue(new Error("nope"));
    await expect(useChat.getState().createConversation()).rejects.toThrow("nope");
    expect(useChat.getState().conversations).toEqual([]);
    expect(useChat.getState().activeId).toBeNull();
  });
});

describe("deleteConversation", () => {
  beforeEach(() => {
    useChat.setState({ conversations: [conv("a"), conv("b")], activeId: "a", messages: { a: [msg("1")], b: [msg("2")] } });
    http.delete.mockResolvedValue({});
  });

  it("removes the conversation and its cached messages; clears activeId only if it was the active one", async () => {
    await useChat.getState().deleteConversation("a");
    expect(http.delete).toHaveBeenCalledWith("/conversations/a");
    const s = useChat.getState();
    expect(s.conversations.map((c) => c.id)).toEqual(["b"]);
    expect(s.messages.a).toBeUndefined();
    expect(s.messages.b).toBeDefined();
    expect(s.activeId).toBeNull();
  });

  it("keeps activeId when a different conversation is deleted", async () => {
    await useChat.getState().deleteConversation("b");
    expect(useChat.getState().activeId).toBe("a");
  });

  it("keeps everything when the server refuses", async () => {
    http.delete.mockRejectedValue(new Error("403"));
    await expect(useChat.getState().deleteConversation("a")).rejects.toThrow();
    expect(useChat.getState().conversations).toHaveLength(2);
  });
});

describe("renameConversation", () => {
  it("PATCHes then updates only that title", async () => {
    useChat.setState({ conversations: [conv("a", "old"), conv("b", "other")] });
    http.patch.mockResolvedValue({});
    await useChat.getState().renameConversation("a", "New title");
    expect(http.patch).toHaveBeenCalledWith("/conversations/a", { title: "New title" });
    expect(useChat.getState().conversations.map((c) => c.title)).toEqual(["New title", "other"]);
  });

  it("does not change the title when the server rejects it", async () => {
    useChat.setState({ conversations: [conv("a", "old")] });
    http.patch.mockRejectedValue(new Error("422"));
    await expect(useChat.getState().renameConversation("a", "")).rejects.toThrow();
    expect(useChat.getState().conversations[0].title).toBe("old");
  });
});

describe("message helpers", () => {
  it("setMessages replaces a transcript", () => {
    useChat.getState().setMessages("c", [msg("1")]);
    useChat.getState().setMessages("c", [msg("2")]);
    expect(useChat.getState().messages.c.map((m) => m.id)).toEqual(["2"]);
  });

  it("addMessage appends in order and creates the transcript when missing", () => {
    useChat.getState().addMessage("c", msg("1"));
    useChat.getState().addMessage("c", msg("2"));
    expect(useChat.getState().messages.c.map((m) => m.id)).toEqual(["1", "2"]);
  });

  it("updateMessage patches only the matching message (e.g. temp id -> real id)", () => {
    useChat.setState({ messages: { c: [msg("tmp", "user", "hello"), msg("x")] } });
    useChat.getState().updateMessage("c", "tmp", { id: "real-1" });
    expect(useChat.getState().messages.c.map((m) => [m.id, m.content])).toEqual([["real-1", "hello"], ["x", "x"]]);
  });

  it("updateMessage on an unknown conversation is a no-op, not a crash", () => {
    expect(() => useChat.getState().updateMessage("nope", "m", { content: "z" })).not.toThrow();
  });

  it("removeMessage drops just that message", () => {
    useChat.setState({ messages: { c: [msg("1"), msg("2")] } });
    useChat.getState().removeMessage("c", "1");
    expect(useChat.getState().messages.c.map((m) => m.id)).toEqual(["2"]);
  });

  it("transcripts of different conversations never mix", () => {
    useChat.getState().addMessage("a", msg("1"));
    useChat.getState().addMessage("b", msg("2"));
    expect(useChat.getState().messages.a).toHaveLength(1);
    expect(useChat.getState().messages.b).toHaveLength(1);
  });
});
