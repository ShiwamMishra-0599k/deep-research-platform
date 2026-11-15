import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

const http = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }));
vi.mock("@/lib/api", () => ({ api: http, apiError: () => "" }));

type Handlers = { onEvent: (m: { event?: string; data: string }) => void; onError?: (e: unknown) => void; onClose?: () => void; signal?: AbortSignal };
const sse = vi.hoisted(() => ({ calls: [] as { path: string; body: any; h: any }[], script: null as null | ((h: any) => Promise<void>) }));
vi.mock("@/lib/sse", async () => {
  const actual = await vi.importActual<typeof import("@/lib/sse")>("@/lib/sse");
  return {
    ...actual,
    postSse: async (path: string, body: unknown, h: Handlers) => {
      sse.calls.push({ path, body, h });
      if (sse.script) await sse.script(h);
    },
  };
});
// Leaflet cannot lay out in jsdom; the block itself is covered in blocks.test.tsx
vi.mock("@/components/blocks/LeafletMapBlock", () => ({ default: () => <div data-testid="map" /> }));

import ChatPanel from "@/components/ChatPanel";
import { SseHttpError } from "@/lib/sse";
import { useChat } from "@/store/chat";
import { useAuth } from "@/store/auth";

const ev = (event: string, data: unknown) => ({ event, data: JSON.stringify(data) });
const BLOCKS = {
  status: "completed",
  data: {
    blocks: [{ template_id: "markdown", data: { content: "Bitcoin is **up**." } }],
    sources: ["https://example.com/a"],
    follow_ups: [{ label: "Compare with Ethereum", query: "Compare BTC and ETH" }],
  },
};

function Where() {
  const l = useLocation();
  return <div data-testid="where">{l.pathname}</div>;
}
function renderPanel(onOpenNav = vi.fn()) {
  render(
    <MemoryRouter initialEntries={["/c/c1"]}>
      <Routes>
        <Route path="*" element={<><ChatPanel onOpenNav={onOpenNav} /><Where /></>} />
      </Routes>
    </MemoryRouter>,
  );
  return { onOpenNav };
}
const box = () => screen.getByRole("textbox", { name: /ask a research question/i });
async function ask(text: string) {
  await userEvent.type(box(), text + "{Enter}");
}
const msgs = (id = "c1") => useChat.getState().messages[id] ?? [];

beforeEach(() => {
  Object.values(http).forEach((f) => f.mockReset());
  http.patch.mockResolvedValue({});
  http.get.mockResolvedValue({ data: [] });
  sse.calls = [];
  sse.script = null;
  useAuth.setState({ user: { id: "u", email: "a@b.co", name: "Ada Lovelace" }, loading: false });
  useChat.setState({ conversations: [{ id: "c1", title: "Existing", createdAt: "t", updatedAt: "t" }], activeId: "c1", messages: { c1: [] }, loading: false });
});

describe("welcome screen", () => {
  it("greets the user by first name and offers suggestions; picking one sends it", async () => {
    sse.script = async () => {};
    renderPanel();
    expect(screen.getByRole("heading", { name: /what would you like to research/i })).toBeInTheDocument();
    expect(screen.getByText(/^Good (morning|afternoon|evening), Ada$/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /bitcoin price in inr/i }));
    await waitFor(() => expect(sse.calls).toHaveLength(1));
    expect(sse.calls[0].body).toEqual({ query: "Bitcoin price in INR" });
  });

  it("header shows 'New research' before any message, then the conversation's (auto-)title", async () => {
    sse.script = async () => {};
    renderPanel();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("New research");
    await ask("hello");
    // the first question of a new conversation becomes its title
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("hello"));
  });

  it("the menu button opens the navigation drawer", async () => {
    const { onOpenNav } = renderPanel();
    await userEvent.click(screen.getByRole("button", { name: "Open navigation" }));
    expect(onOpenNav).toHaveBeenCalledTimes(1);
  });
});

describe("sending a question", () => {
  it("POSTs to the active conversation's query endpoint with the text", async () => {
    sse.script = async () => {};
    renderPanel();
    await ask("EV chargers in Lucknow");
    await waitFor(() => expect(sse.calls).toHaveLength(1));
    expect(sse.calls[0].path).toBe("/conversations/c1/query");
    expect(sse.calls[0].body).toEqual({ query: "EV chargers in Lucknow" });
  });

  it("adds the user's message and an assistant placeholder immediately (optimistic UI)", async () => {
    let release!: () => void;
    sse.script = () => new Promise<void>((r) => { release = r; });
    renderPanel();
    await ask("question one");
    await waitFor(() => expect(msgs()).toHaveLength(2));
    expect(msgs().map((m) => m.role)).toEqual(["user", "assistant"]);
    expect(msgs()[0].content).toBe("question one");
    expect(screen.getByRole("button", { name: /stop generating/i })).toBeInTheDocument(); // busy
    await act(async () => release());
    await waitFor(() => expect(screen.queryByRole("button", { name: /stop generating/i })).not.toBeInTheDocument());
  });

  it("titles a brand-new conversation from the first question (truncated to 60 chars)", async () => {
    sse.script = async () => {};
    renderPanel();
    await ask("x".repeat(80));
    await waitFor(() => expect(http.patch).toHaveBeenCalled());
    expect(http.patch).toHaveBeenCalledWith("/conversations/c1", { title: "x".repeat(57) + "..." });
  });

  it("does not retitle an existing conversation", async () => {
    useChat.setState({ messages: { c1: [{ id: "old", role: "user", content: "earlier", createdAt: "t" }] } });
    sse.script = async () => {};
    renderPanel();
    await ask("second question");
    await waitFor(() => expect(sse.calls).toHaveLength(1));
    expect(http.patch).not.toHaveBeenCalled();
  });

  it("with no active conversation it creates one, routes to /c/:id, then sends", async () => {
    useChat.setState({ activeId: null, messages: {}, conversations: [] });
    http.post.mockResolvedValue({ data: { id: "new-1", title: "New chat", createdAt: "t", updatedAt: "t" } });
    http.get.mockResolvedValue({ data: [] });
    sse.script = async () => {};
    renderPanel();
    await ask("first ever");
    await waitFor(() => expect(sse.calls).toHaveLength(1));
    expect(http.post).toHaveBeenCalledWith("/conversations", {});
    expect(sse.calls[0].path).toBe("/conversations/new-1/query");
    expect(screen.getByTestId("where")).toHaveTextContent("/c/new-1");
  });
});

describe("streaming events", () => {
  it("user_message and persisted swap temporary ids for the stored ones", async () => {
    sse.script = async (h) => {
      h.onEvent(ev("user_message", { id: "user-db-1" }));
      h.onEvent(ev("blocks", BLOCKS));
      h.onEvent(ev("persisted", { messageId: "asst-db-1" }));
    };
    renderPanel();
    await ask("hi");
    await waitFor(() => expect(msgs().map((m) => m.id)).toEqual(["user-db-1", "asst-db-1"]));
  });

  it("checkpoint events appear live in the progress trail", async () => {
    let release!: () => void;
    sse.script = (h) => {
      h.onEvent(ev("checkpoint", { status: "in_progress", content: "Searching the web for: x" }));
      h.onEvent(ev("checkpoint", { status: "in_progress", content: "Found 5 results. Analyzing...", tool: "web_search" }));
      return new Promise<void>((r) => { release = r; });
    };
    renderPanel();
    await ask("hi");
    const trail = await screen.findByRole("region", { name: "Research progress" });
    expect(within(trail).getAllByRole("listitem").map((li) => li.textContent)).toEqual(
      expect.arrayContaining([expect.stringContaining("Searching the web for: x"), expect.stringContaining("Found 5 results")]),
    );
    await act(async () => release());
    await waitFor(() => expect(screen.queryByRole("region", { name: "Research progress" })).not.toBeInTheDocument()); // cleared when done
  });

  it("blocks event renders the answer, citations and follow-up suggestions", async () => {
    sse.script = async (h) => h.onEvent(ev("blocks", BLOCKS));
    renderPanel();
    await ask("btc");
    expect(await screen.findByText("up")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /example\.com/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Compare with Ethereum" })).toBeInTheDocument();
    expect(msgs()[1].content).toBe("Bitcoin is **up**.");
    expect(msgs()[1].sources).toEqual(["https://example.com/a"]);
  });

  it("clicking a follow-up sends that suggestion's query as a new question", async () => {
    sse.script = async (h) => h.onEvent(ev("blocks", BLOCKS));
    renderPanel();
    await ask("btc");
    await userEvent.click(await screen.findByRole("button", { name: "Compare with Ethereum" }));
    await waitFor(() => expect(sse.calls).toHaveLength(2));
    expect(sse.calls[1].body).toEqual({ query: "Compare BTC and ETH" });
  });

  it("clarification event shows the agent's question as the answer", async () => {
    sse.script = async (h) => h.onEvent(ev("clarification", { status: "completed", content: "Which city are you searching in?" }));
    renderPanel();
    await ask("charging stations");
    expect(await screen.findByText("Which city are you searching in?")).toBeInTheDocument();
  });

  it("error event shows the (already safe) server message flagged as an error", async () => {
    sse.script = async (h) => h.onEvent(ev("error", { status: "failed", content: "The research service is currently unavailable. Please try again shortly." }));
    renderPanel();
    await ask("x");
    expect(await screen.findByText(/the research service is currently unavailable/i)).toBeInTheDocument();
    expect(screen.getByText(/^Error:/)).toBeInTheDocument();
  });

  it("persist_error keeps the answer and appends a notice that it was not saved", async () => {
    sse.script = async (h) => {
      h.onEvent(ev("blocks", BLOCKS));
      h.onEvent(ev("persist_error", { status: "failed", content: "Could not save this answer." }));
    };
    renderPanel();
    await ask("x");
    expect(await screen.findByText(/could not be saved to your history/i)).toBeInTheDocument();
    expect(screen.getByText("up")).toBeInTheDocument(); // the answer is still there
  });

  it("ignores events without a name and malformed JSON payloads instead of crashing", async () => {
    sse.script = async (h) => {
      h.onEvent({ data: "keepalive" });
      h.onEvent({ event: "checkpoint", data: "{not json" });
      h.onEvent(ev("blocks", BLOCKS));
    };
    renderPanel();
    await ask("x");
    expect(await screen.findByText("up")).toBeInTheDocument();
  });
});

describe("transport and HTTP errors map to specific, helpful messages", () => {
  const cases: [string, unknown, RegExp][] = [
    ["429", new SseHttpError(429), /sending requests too quickly/i],
    ["401", new SseHttpError(401), /session has expired.*sign in again/i],
    ["404", new SseHttpError(404), /conversation no longer exists/i],
    ["413", new SseHttpError(413), /request was rejected.*shorter question/i],
    ["422", new SseHttpError(422), /request was rejected.*shorter question/i],
    ["500", new SseHttpError(500), /temporarily unavailable/i],
    ["503", new SseHttpError(503), /temporarily unavailable/i],
    ["plain network failure", new TypeError("Failed to fetch"), /connection lost\. please retry/i],
  ];
  it.each(cases)("%s", async (_label, error, expected) => {
    sse.script = async (h) => { h.onError?.(error); throw error; };
    vi.spyOn(console, "error").mockImplementation(() => {});
    renderPanel();
    await ask("x");
    expect(await screen.findByText(expected)).toBeInTheDocument();
    expect(msgs()[1].content).toMatch(expected);
    expect(screen.queryByRole("button", { name: /stop generating/i })).not.toBeInTheDocument(); // no longer busy
  });

  it("a failed request can simply be retried afterwards", async () => {
    sse.script = async (h) => { const e = new SseHttpError(429); h.onError?.(e); throw e; };
    vi.spyOn(console, "error").mockImplementation(() => {});
    renderPanel();
    await ask("first");
    await screen.findByText(/too quickly/i);
    sse.script = async (h) => h.onEvent(ev("blocks", BLOCKS));
    await ask("second");
    expect(await screen.findByText("up")).toBeInTheDocument();
  });
});

describe("stopping", () => {
  it("Stop aborts the request's signal and marks the answer as stopped by the user", async () => {
    let signal!: AbortSignal;
    sse.script = (h) => new Promise<void>((resolve) => { signal = h.signal; h.signal.addEventListener("abort", () => resolve()); });
    renderPanel();
    await ask("long question");
    await userEvent.click(await screen.findByRole("button", { name: /stop generating/i }));
    expect(signal.aborted).toBe(true);
    expect(await screen.findByText(/stopped by user/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("button", { name: /stop generating/i })).not.toBeInTheDocument());
  });
});

describe("greeting by time of day", () => {
  it.each([[8, "Good morning"], [14, "Good afternoon"], [20, "Good evening"], [3, "Good morning"]])("at %i:00 says '%s'", (hour, expected) => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(2026, 10, 15, hour, 0, 0));
    renderPanel();
    expect(screen.getByText(new RegExp("^" + expected + ", Ada$"))).toBeInTheDocument();
  });

  it("greets without a name when the user has none", () => {
    useAuth.setState({ user: null });
    renderPanel();
    expect(screen.getByText(/^Good (morning|afternoon|evening)$/)).toBeInTheDocument();
  });
});

describe("auto-scroll", () => {
  function logEl() {
    return screen.getByRole("log", { name: "Conversation" });
  }
  function setMetrics(el: HTMLElement, m: { scrollHeight: number; clientHeight: number; scrollTop: number }) {
    for (const [k, v] of Object.entries(m)) Object.defineProperty(el, k, { configurable: true, value: v });
  }

  it("follows new progress while the user is at the bottom, but does NOT yank them back after they scroll up", async () => {
    const scrollTo = vi.spyOn(Element.prototype, "scrollTo").mockImplementation(() => {});
    let h!: Handlers;
    let release!: () => void;
    sse.script = (handlers: Handlers) => { h = handlers; return new Promise<void>((r) => { release = r; }); };
    renderPanel();
    await ask("question");
    await screen.findByRole("button", { name: /stop generating/i });

    // user scrolls up to read earlier content (600px from the bottom)
    scrollTo.mockClear();
    setMetrics(logEl(), { scrollHeight: 1000, clientHeight: 400, scrollTop: 0 });
    act(() => { logEl().dispatchEvent(new Event("scroll")); });
    act(() => h.onEvent(ev("checkpoint", { status: "in_progress", content: "step A" })));
    await screen.findByText("step A");
    expect(scrollTo).not.toHaveBeenCalled();

    // user returns to the bottom: following resumes, instantly (no smooth animation) while streaming
    setMetrics(logEl(), { scrollHeight: 1000, clientHeight: 400, scrollTop: 590 });
    act(() => { logEl().dispatchEvent(new Event("scroll")); });
    act(() => h.onEvent(ev("checkpoint", { status: "in_progress", content: "step B" })));
    await screen.findByText("step B");
    await waitFor(() => expect(scrollTo).toHaveBeenCalled());
    expect(scrollTo).toHaveBeenLastCalledWith({ top: 1000, behavior: "auto" });
    await act(async () => release());
  });

  it("a NEW question always re-engages following, even if the user had scrolled up", async () => {
    const scrollTo = vi.spyOn(Element.prototype, "scrollTo").mockImplementation(() => {});
    sse.script = async () => {};
    renderPanel();
    setMetrics(logEl(), { scrollHeight: 1000, clientHeight: 400, scrollTop: 0 });
    act(() => { logEl().dispatchEvent(new Event("scroll")); });
    scrollTo.mockClear();
    await ask("another question");
    await waitFor(() => expect(scrollTo).toHaveBeenCalled());
  });
});
