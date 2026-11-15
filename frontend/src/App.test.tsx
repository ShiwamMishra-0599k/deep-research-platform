import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const http = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }));
vi.mock("@/lib/api", async () => ({ ...(await vi.importActual<typeof import("@/lib/api")>("@/lib/api")), api: http }));
vi.mock("@/lib/sse", () => ({ postSse: vi.fn(), SseHttpError: class extends Error {} }));
vi.mock("@/components/blocks/LeafletMapBlock", () => ({ default: () => null }));

import App from "@/App";
import { useAuth } from "@/store/auth";
import { useChat } from "@/store/chat";

const user = { id: "u1", email: "ada@example.com", name: "Ada" };
function renderApp(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);
}

beforeEach(() => {
  Object.values(http).forEach((f) => f.mockReset());
  http.get.mockImplementation(async (u: string) => (u === "/conversations" ? { data: [{ id: "c1", title: "Chat one", createdAt: "t", updatedAt: "t" }] } : { data: [] }));
  useAuth.setState({ user: null, loading: false });
  useChat.setState({ conversations: [], activeId: null, messages: {}, loading: false });
});

describe("route protection", () => {
  it.each(["/", "/c/c1", "/some/unknown/path"])("signed out: %s redirects to the sign-in page and never shows the workspace", (path) => {
    renderApp(path);
    expect(screen.getByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /ask a research question/i })).not.toBeInTheDocument();
    expect(http.get).not.toHaveBeenCalled(); // no API traffic without a session
  });

  it("signed in: / shows the workspace", async () => {
    useAuth.setState({ user });
    renderApp("/");
    expect(await screen.findByRole("textbox", { name: /ask a research question/i })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Chat one" })).toBeInTheDocument();
  });

  it("signed in: /c/:id shows the workspace for that conversation", async () => {
    useAuth.setState({ user });
    http.get.mockImplementation(async (u: string) => {
      if (u === "/conversations") return { data: [{ id: "c1", title: "Chat one", createdAt: "t", updatedAt: "t" }] };
      return { data: [{ id: "m1", role: "user", content: "an earlier question", createdAt: "t" }] };
    });
    renderApp("/c/c1");
    expect(await screen.findByText("an earlier question")).toBeInTheDocument();
  });

  it("signed in: unknown paths fall back to the workspace, not a blank page", async () => {
    useAuth.setState({ user });
    renderApp("/nope");
    expect(await screen.findByRole("textbox", { name: /ask a research question/i })).toBeInTheDocument();
  });

  it("the sign-in and register pages are reachable while signed out", () => {
    renderApp("/register");
    expect(screen.getByRole("heading", { level: 1, name: "Create your account" })).toBeInTheDocument();
  });
});

describe("session hydration", () => {
  it("REGRESSION (login flash on reload): with a stored session the very first render is the workspace, never the sign-in page", async () => {
    localStorage.setItem("auth_user", JSON.stringify(user));
    vi.resetModules();
    // fresh module graph = a browser reload: the store reads localStorage at creation
    const { default: FreshApp } = await import("@/App");
    const seen: string[] = [];
    const { MemoryRouter: MR } = await import("react-router-dom");
    const probe = new MutationObserver(() => {
      if (document.body.textContent?.includes("Create your account") || document.querySelector("h1")?.textContent === "Sign in") seen.push("login");
    });
    probe.observe(document.body, { childList: true, subtree: true });
    render(<MR initialEntries={["/"]}><FreshApp /></MR>);
    expect(document.querySelector("h1")?.textContent).not.toBe("Sign in");
    expect(await screen.findByRole("textbox", { name: /ask a research question/i })).toBeInTheDocument();
    probe.disconnect();
    expect(seen).toEqual([]);
  });
});
