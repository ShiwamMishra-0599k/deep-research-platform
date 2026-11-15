import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

const http = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }));
vi.mock("@/lib/api", () => ({ api: http, apiError: () => "" }));
vi.mock("@/lib/sse", () => ({ postSse: vi.fn(), SseHttpError: class extends Error {} }));
vi.mock("@/components/blocks/LeafletMapBlock", () => ({ default: () => null }));

import Chat from "@/pages/Chat";
import { useAuth } from "@/store/auth";
import { useChat } from "@/store/chat";

beforeEach(() => {
  Object.values(http).forEach((f) => f.mockReset());
  http.get.mockResolvedValue({ data: [] });
  useAuth.setState({ user: { id: "u", email: "a@b.co", name: "Ada" } });
  useChat.setState({ conversations: [], activeId: null, messages: {}, loading: false });
});

const renderChat = () => render(<MemoryRouter><Chat /></MemoryRouter>);

describe("Chat page shell", () => {
  it("offers a skip link that targets the main conversation region", () => {
    renderChat();
    const skip = screen.getByRole("link", { name: "Skip to conversation" });
    expect(skip).toHaveAttribute("href", "#main");
    expect(document.getElementById("main")).toBeInTheDocument();
  });

  it("the menu button opens the mobile drawer; its backdrop closes it", async () => {
    renderChat();
    expect(screen.queryByRole("button", { name: "Close navigation" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Open navigation" }));
    expect(document.getElementById("app-nav")!.className).not.toContain("invisible");
    await userEvent.click(screen.getByRole("button", { name: "Close navigation" }));
    expect(screen.queryByRole("button", { name: "Close navigation" })).not.toBeInTheDocument();
    expect(document.getElementById("app-nav")!.className).toContain("invisible");
  });

  it("Escape closes the open drawer", async () => {
    renderChat();
    await userEvent.click(screen.getByRole("button", { name: "Open navigation" }));
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("button", { name: "Close navigation" })).not.toBeInTheDocument();
  });

  it("Escape does nothing (and no listener is left behind) while the drawer is closed", async () => {
    const add = vi.spyOn(window, "addEventListener");
    renderChat();
    expect(add.mock.calls.filter(([t]) => t === "keydown")).toHaveLength(0);
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("button", { name: "Close navigation" })).not.toBeInTheDocument();
  });

  it("removes its Escape listener once the drawer closes", async () => {
    const remove = vi.spyOn(window, "removeEventListener");
    renderChat();
    await userEvent.click(screen.getByRole("button", { name: "Open navigation" }));
    await userEvent.keyboard("{Escape}");
    expect(remove.mock.calls.some(([t]) => t === "keydown")).toBe(true);
  });
});
