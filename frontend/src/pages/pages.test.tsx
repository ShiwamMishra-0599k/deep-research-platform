import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

const http = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: http }; // keep the real apiError, replace only the network client
});
vi.mock("@/lib/sse", () => ({ postSse: vi.fn(), SseHttpError: class extends Error {} }));

import Login from "@/pages/Login";
import Register from "@/pages/Register";
import { useAuth } from "@/store/auth";
import { useChat } from "@/store/chat";

function Where() {
  return <div data-testid="where">{useLocation().pathname}</div>;
}
function renderAt(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/register" element={<Register />} />
        <Route path="*" element={<Where />} />
      </Routes>
      <Where />
    </MemoryRouter>,
  );
}
const where = () => screen.getAllByTestId("where").at(-1)!.textContent;

beforeEach(() => {
  Object.values(http).forEach((f) => f.mockReset());
  useAuth.setState({ user: null, loading: false });
  useChat.setState({ conversations: [], activeId: null, messages: {}, loading: false });
});

describe("Login page", () => {
  it("has labelled, correctly typed fields for password managers and screen readers", () => {
    renderAt("/login");
    expect(screen.getByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toHaveAttribute("type", "email");
    expect(screen.getByLabelText("Email")).toHaveAttribute("autocomplete", "email");
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
    expect(screen.getByLabelText("Password")).toHaveAttribute("autocomplete", "current-password");
    expect(screen.getByLabelText("Email")).toBeRequired();
  });

  it("successful sign-in stores the session and goes to the workspace", async () => {
    http.post.mockResolvedValue({ data: { token: "tok", id: "u1", email: "ada@example.com", name: "Ada" } });
    renderAt("/login");
    await userEvent.type(screen.getByLabelText("Email"), "ada@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "correct-horse-1{Enter}");
    await waitFor(() => expect(where()).toBe("/"));
    expect(http.post).toHaveBeenCalledWith("/auth/login", { email: "ada@example.com", password: "correct-horse-1" });
    expect(localStorage.getItem("auth_token")).toBe("tok");
    expect(useAuth.getState().user?.name).toBe("Ada");
  });

  it("shows the SERVER's message (e.g. Invalid credentials) in an alert and stays on the page", async () => {
    http.post.mockRejectedValue({ response: { status: 401, data: { detail: "Invalid credentials" } } });
    renderAt("/login");
    await userEvent.type(screen.getByLabelText("Email"), "a@b.co");
    await userEvent.type(screen.getByLabelText("Password"), "wrong-pass{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid credentials");
    expect(where()).toBe("/login");
    expect(localStorage.getItem("auth_token")).toBeNull();
  });

  it("rate limiting: shows the server's too-many-attempts message", async () => {
    http.post.mockRejectedValue({ response: { status: 429, data: { detail: "Too many attempts. Try again later." } } });
    renderAt("/login");
    await userEvent.type(screen.getByLabelText("Email"), "a@b.co");
    await userEvent.type(screen.getByLabelText("Password"), "x{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("Too many attempts. Try again later.");
  });

  it("network failure shows a generic fallback, never a raw error object", async () => {
    http.post.mockRejectedValue(new Error("Network Error"));
    renderAt("/login");
    await userEvent.type(screen.getByLabelText("Email"), "a@b.co");
    await userEvent.type(screen.getByLabelText("Password"), "x{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent(/sign-in failed/i);
  });

  it("while the request is pending: button disabled with 'Signing in…'; afterwards the previous error is cleared on resubmit", async () => {
    let finish!: (v: unknown) => void;
    http.post.mockImplementationOnce(() => Promise.reject({ response: { data: { detail: "Invalid credentials" } } }));
    renderAt("/login");
    await userEvent.type(screen.getByLabelText("Email"), "a@b.co");
    await userEvent.type(screen.getByLabelText("Password"), "x{Enter}");
    await screen.findByRole("alert");

    http.post.mockImplementationOnce(() => new Promise((r) => { finish = r; }));
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    const busy = await screen.findByRole("button", { name: /signing in/i });
    expect(busy).toBeDisabled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument(); // old error cleared immediately
    finish({ data: { token: "t", id: "1", email: "a@b.co", name: "A" } });
    await waitFor(() => expect(where()).toBe("/"));
  });

  it("links to registration", async () => {
    renderAt("/login");
    await userEvent.click(screen.getByRole("link", { name: /create an account/i }));
    expect(where()).toBe("/register");
  });
});

describe("Register page", () => {
  it("collects name, email and a new-password with an 8-character minimum and hint", () => {
    renderAt("/register");
    expect(screen.getByLabelText("Name")).toBeRequired();
    expect(screen.getByLabelText("Password")).toHaveAttribute("autocomplete", "new-password");
    expect(screen.getByLabelText("Password")).toHaveAttribute("minlength", "8");
    expect(screen.getByLabelText("Password")).toHaveAccessibleDescription("At least 8 characters.");
  });

  it("successful registration signs the user in and goes to the workspace", async () => {
    http.post.mockResolvedValue({ data: { token: "tok2", id: "u2", email: "bob@example.com", name: "Bob" } });
    renderAt("/register");
    await userEvent.type(screen.getByLabelText("Name"), "Bob");
    await userEvent.type(screen.getByLabelText("Email"), "bob@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "longenough1{Enter}");
    await waitFor(() => expect(where()).toBe("/"));
    expect(http.post).toHaveBeenCalledWith("/auth/register", { email: "bob@example.com", password: "longenough1", name: "Bob" });
    expect(localStorage.getItem("auth_token")).toBe("tok2");
  });

  it("duplicate email: shows the server message", async () => {
    http.post.mockRejectedValue({ response: { status: 409, data: { detail: "Email already registered" } } });
    renderAt("/register");
    await userEvent.type(screen.getByLabelText("Name"), "Bob");
    await userEvent.type(screen.getByLabelText("Email"), "bob@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "longenough1{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("Email already registered");
    expect(where()).toBe("/register");
  });

  it("validation errors (a list) show the first readable message", async () => {
    http.post.mockRejectedValue({ response: { status: 422, data: { detail: [{ loc: ["body", "password"], msg: "String should have at least 8 characters" }] } } });
    renderAt("/register");
    await userEvent.type(screen.getByLabelText("Name"), "B");
    await userEvent.type(screen.getByLabelText("Email"), "b@b.co");
    await userEvent.type(screen.getByLabelText("Password"), "12345678{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("String should have at least 8 characters");
  });

  it("shows a pending state while creating the account", async () => {
    http.post.mockReturnValue(new Promise(() => {}));
    renderAt("/register");
    await userEvent.type(screen.getByLabelText("Name"), "Bob");
    await userEvent.type(screen.getByLabelText("Email"), "b@b.co");
    await userEvent.type(screen.getByLabelText("Password"), "longenough1{Enter}");
    expect(await screen.findByRole("button", { name: /creating account/i })).toBeDisabled();
  });
});
