import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

const http = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }));
vi.mock("@/lib/api", () => ({ api: http, apiError: () => "", onSessionEnded: () => () => {} }));

import ConversationSidebar from "@/components/ConversationSidebar";
import { useAuth } from "@/store/auth";
import { useChat } from "@/store/chat";

const conv = (id: string, title: string) => ({ id, title, createdAt: "t", updatedAt: "t" });
const LIST = [conv("c1", "EV chargers in Lucknow"), conv("c2", "Bitcoin in INR")];

function Where() {
  return <div data-testid="where">{useLocation().pathname}</div>;
}
function renderSidebar(path = "/c/c1", props: { open?: boolean; onClose?: () => void } = {}) {
  const onClose = props.onClose ?? vi.fn();
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/c/:id" element={<><ConversationSidebar open={props.open ?? false} onClose={onClose} /><Where /></>} />
        <Route path="*" element={<><ConversationSidebar open={props.open ?? false} onClose={onClose} /><Where /></>} />
      </Routes>
    </MemoryRouter>,
  );
  return { onClose };
}
const nav = () => screen.getByRole("navigation", { name: "Conversations" });
const where = () => screen.getByTestId("where").textContent;

beforeEach(() => {
  Object.values(http).forEach((f) => f.mockReset());
  http.get.mockImplementation(async (url: string) => (url === "/conversations" ? { data: LIST } : { data: [] }));
  http.post.mockResolvedValue({ data: {} }); // /auth/logout
  useAuth.setState({ user: { id: "u1", email: "ada@example.com", name: "Ada" }, status: "authenticated", loading: false, booted: true });
  useChat.setState({ conversations: [], activeId: null, messages: {}, loading: false });
});

describe("conversation list", () => {
  it("loads the user's conversations and marks the one in the URL as current", async () => {
    renderSidebar("/c/c1");
    await within(nav()).findByRole("button", { name: "EV chargers in Lucknow" });
    expect(within(nav()).getByRole("button", { name: "EV chargers in Lucknow" })).toHaveAttribute("aria-current", "page");
    expect(within(nav()).getByRole("button", { name: "Bitcoin in INR" })).not.toHaveAttribute("aria-current");
    expect(http.get).toHaveBeenCalledWith("/conversations");
  });

  it("shows an empty-state message when there are none", async () => {
    http.get.mockResolvedValue({ data: [] });
    renderSidebar("/");
    expect(await screen.findByText("No conversations yet.")).toBeInTheDocument();
  });

  it("clicking a conversation navigates to it and closes the mobile drawer", async () => {
    const { onClose } = renderSidebar("/c/c1");
    await userEvent.click(await within(nav()).findByRole("button", { name: "Bitcoin in INR" }));
    expect(where()).toBe("/c/c2");
    expect(onClose).toHaveBeenCalled();
  });

  it("REGRESSION (stale conversation): opening a URL for a conversation that cannot be loaded redirects home", async () => {
    http.get.mockImplementation(async (url: string) => {
      if (url === "/conversations") return { data: LIST };
      throw { response: { status: 404 } };
    });
    renderSidebar("/c/gone");
    await waitFor(() => expect(where()).toBe("/"));
    expect(useChat.getState().activeId).toBeNull();
  });

  it("a failing conversation list request does not crash the page (401 is handled globally)", async () => {
    http.get.mockRejectedValue({ response: { status: 401 } });
    renderSidebar("/");
    expect(await screen.findByText("No conversations yet.")).toBeInTheDocument();
  });
});

describe("new chat", () => {
  it("creates a conversation, routes to it and closes the drawer", async () => {
    http.post.mockResolvedValue({ data: conv("c9", "New chat") });
    const { onClose } = renderSidebar("/");
    await userEvent.click(screen.getByRole("button", { name: /new chat/i }));
    await waitFor(() => expect(where()).toBe("/c/c9"));
    expect(http.post).toHaveBeenCalledWith("/conversations", {});
    expect(onClose).toHaveBeenCalled();
  });
});

describe("deleting", () => {
  it("opens an accessible confirmation dialog (never the browser confirm) naming the conversation", async () => {
    const confirmSpy = vi.spyOn(window, "confirm");
    renderSidebar("/c/c1");
    await within(nav()).findByRole("button", { name: "Bitcoin in INR" });
    await userEvent.click(screen.getByRole("button", { name: "Delete conversation: Bitcoin in INR" }));
    const dlg = screen.getByRole("alertdialog", { name: "Delete this conversation?" });
    expect(dlg).toHaveTextContent("Bitcoin in INR");
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(http.delete).not.toHaveBeenCalled(); // nothing happens until confirmed
  });

  it("Cancel closes the dialog and deletes nothing", async () => {
    renderSidebar("/c/c1");
    await userEvent.click(await screen.findByRole("button", { name: "Delete conversation: Bitcoin in INR" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(http.delete).not.toHaveBeenCalled();
    expect(useChat.getState().conversations).toHaveLength(2);
  });

  it("Confirm deletes via the API and removes it from the list", async () => {
    http.delete.mockResolvedValue({});
    renderSidebar("/c/c1");
    await userEvent.click(await screen.findByRole("button", { name: "Delete conversation: Bitcoin in INR" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(http.delete).toHaveBeenCalledWith("/conversations/c2"));
    await waitFor(() => expect(within(nav()).queryByRole("button", { name: "Bitcoin in INR" })).not.toBeInTheDocument());
    expect(where()).toBe("/c/c1"); // a non-active conversation was deleted: stay put
  });

  it("deleting the ACTIVE conversation sends the user back to the welcome route", async () => {
    http.delete.mockResolvedValue({});
    renderSidebar("/c/c1");
    await userEvent.click(await screen.findByRole("button", { name: "Delete conversation: EV chargers in Lucknow" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(where()).toBe("/"));
    expect(useChat.getState().conversations.map((c) => c.id)).toEqual(["c2"]);
  });
});

describe("account and drawer", () => {
  it("shows the signed-in user", async () => {
    renderSidebar("/");
    expect(screen.getByText("Ada")).toBeInTheDocument();
    expect(screen.getByText("ada@example.com")).toBeInTheDocument();
  });

  it("Sign out clears credentials and chat state, and goes to /login", async () => {
    localStorage.setItem("auth_token", "t");
    localStorage.setItem("auth_user", "{}");
    renderSidebar("/c/c1");
    await within(nav()).findByRole("button", { name: "Bitcoin in INR" });
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(where()).toBe("/login");
    expect(http.post).toHaveBeenCalledWith("/auth/logout"); // server-side revocation, not just a local reset
    expect(localStorage.getItem("auth_token")).toBeNull();
    expect(useAuth.getState().user).toBeNull();
    expect(useAuth.getState().status).toBe("anonymous");
    expect(useChat.getState().conversations).toEqual([]);
  });

  it("the drawer backdrop exists only while open and closes it when clicked", async () => {
    const { onClose } = renderSidebar("/", { open: true });
    await userEvent.click(screen.getByRole("button", { name: "Close navigation" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("no backdrop while closed; the closed drawer is hidden from assistive tech and keyboard (invisible)", () => {
    renderSidebar("/", { open: false });
    expect(screen.queryByRole("button", { name: "Close navigation" })).not.toBeInTheDocument();
    expect(document.getElementById("app-nav")!.className).toContain("invisible");
  });
});
