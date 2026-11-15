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
  useAuth.setState({ user: null, status: "anonymous", loading: false, booted: true });
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
    useAuth.setState({ user, status: "authenticated", booted: true });
    renderApp("/");
    expect(await screen.findByRole("textbox", { name: /ask a research question/i })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Chat one" })).toBeInTheDocument();
  });

  it("signed in: /c/:id shows the workspace for that conversation", async () => {
    useAuth.setState({ user, status: "authenticated", booted: true });
    http.get.mockImplementation(async (u: string) => {
      if (u === "/conversations") return { data: [{ id: "c1", title: "Chat one", createdAt: "t", updatedAt: "t" }] };
      return { data: [{ id: "m1", role: "user", content: "an earlier question", createdAt: "t" }] };
    });
    renderApp("/c/c1");
    expect(await screen.findByText("an earlier question")).toBeInTheDocument();
  });

  it("signed in: unknown paths fall back to the workspace, not a blank page", async () => {
    useAuth.setState({ user, status: "authenticated", booted: true });
    renderApp("/nope");
    expect(await screen.findByRole("textbox", { name: /ask a research question/i })).toBeInTheDocument();
  });

  it("the sign-in and register pages are reachable while signed out", () => {
    renderApp("/register");
    expect(screen.getByRole("heading", { level: 1, name: "Create your account" })).toBeInTheDocument();
  });
});

describe("session boot (cookie sessions)", () => {
  async function freshApp() {
    vi.resetModules();
    const { default: FreshApp } = await import("@/App");
    const { MemoryRouter: MR } = await import("react-router-dom");
    return { FreshApp, MR };
  }

  it("REGRESSION (login flash on reload): while /auth/me is pending a neutral loading state shows, never the sign-in page or the workspace", async () => {
    let answer!: (v: unknown) => void;
    http.get.mockImplementation((u: string) => (u === "/auth/me" ? new Promise((r) => { answer = r; }) : Promise.resolve({ data: [] })));
    const { FreshApp, MR } = await freshApp();
    render(<MR initialEntries={["/"]}><FreshApp /></MR>);
    expect(screen.getByRole("status")).toHaveAttribute("aria-busy", "true");
    expect(document.querySelector("h1")).toBeNull(); // no "Sign in" heading
    expect(screen.queryByRole("textbox", { name: /ask a research question/i })).not.toBeInTheDocument();
    answer({ data: user });
    expect(await screen.findByRole("textbox", { name: /ask a research question/i })).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("valid cookie session on reload: straight to the workspace, never the sign-in page", async () => {
    http.get.mockImplementation(async (u: string) => (u === "/auth/me" ? { data: user } : { data: [] }));
    const { FreshApp, MR } = await freshApp();
    const seenLogin: string[] = [];
    const probe = new MutationObserver(() => { if (document.querySelector("h1")?.textContent === "Sign in") seenLogin.push("login"); });
    probe.observe(document.body, { childList: true, subtree: true });
    render(<MR initialEntries={["/c/c1"]}><FreshApp /></MR>);
    expect(await screen.findByRole("textbox", { name: /ask a research question/i })).toBeInTheDocument();
    probe.disconnect();
    expect(seenLogin).toEqual([]);
  });

  it("no session (401 from /auth/me): lands on the sign-in page", async () => {
    http.get.mockRejectedValue({ response: { status: 401 } });
    const { FreshApp, MR } = await freshApp();
    render(<MR initialEntries={["/c/c1"]}><FreshApp /></MR>);
    expect(await screen.findByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
  });

  it("legacy localStorage token: upgraded to a cookie session on first load, then removed from storage", async () => {
    localStorage.setItem("auth_token", "legacy");
    localStorage.setItem("auth_user", JSON.stringify(user));
    http.post.mockResolvedValue({ data: user });
    http.get.mockImplementation(async () => ({ data: [] }));
    const { FreshApp, MR } = await freshApp();
    render(<MR initialEntries={["/"]}><FreshApp /></MR>);
    expect(await screen.findByRole("textbox", { name: /ask a research question/i })).toBeInTheDocument();
    expect(http.post).toHaveBeenCalledWith("/auth/session/upgrade", undefined, { headers: { Authorization: "Bearer legacy" } });
    expect(localStorage.getItem("auth_token")).toBeNull();
    expect(localStorage.getItem("auth_user")).toBeNull();
  });

  it("the sign-in and register pages do not wait for the boot request", async () => {
    http.get.mockReturnValue(new Promise(() => {}));
    const { FreshApp, MR } = await freshApp();
    render(<MR initialEntries={["/login"]}><FreshApp /></MR>);
    expect(screen.getByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
  });
});
