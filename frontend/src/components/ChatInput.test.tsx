import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ChatInput from "@/components/ChatInput";

const box = () => screen.getByRole("textbox", { name: /ask a research question/i });

describe("ChatInput", () => {
  it("Enter sends the trimmed text and clears the box", async () => {
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} />);
    await userEvent.type(box(), "  Bitcoin price  {Enter}");
    expect(onSend).toHaveBeenCalledExactlyOnceWith("Bitcoin price");
    expect(box()).toHaveValue("");
  });

  it("Shift+Enter inserts a newline and does NOT send", async () => {
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} />);
    await userEvent.type(box(), "line one{Shift>}{Enter}{/Shift}line two");
    expect(onSend).not.toHaveBeenCalled();
    expect(box()).toHaveValue("line one\nline two");
  });

  it("ignores Enter while an IME composition is in progress (isComposing)", () => {
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} />);
    fireEvent.change(box(), { target: { value: "にほん" } });
    fireEvent.keyDown(box(), { key: "Enter", isComposing: true });
    expect(onSend).not.toHaveBeenCalled();
    expect(box()).toHaveValue("にほん");
  });

  it("ignores Enter with keyCode 229 (mobile predictive text / IME)", () => {
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} />);
    fireEvent.change(box(), { target: { value: "hello" } });
    fireEvent.keyDown(box(), { key: "Enter", keyCode: 229 });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("never sends empty or whitespace-only text, and the send button is disabled", async () => {
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} />);
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    await userEvent.type(box(), "   {Enter}");
    expect(onSend).not.toHaveBeenCalled();
  });

  it("the Send button submits the form", async () => {
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} />);
    await userEvent.type(box(), "hello");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(onSend).toHaveBeenCalledWith("hello");
  });

  it("while busy: shows Stop (not Send), Enter does not send, and Stop calls onStop", async () => {
    const onSend = vi.fn();
    const onStop = vi.fn();
    render(<ChatInput onSend={onSend} onStop={onStop} busy />);
    expect(screen.queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
    await userEvent.type(box(), "queued{Enter}");
    expect(onSend).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: /stop generating/i }));
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("busy without an onStop handler falls back to a (disabled) Send button", () => {
    render(<ChatInput onSend={() => {}} busy />);
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("uses a custom placeholder when given", () => {
    render(<ChatInput onSend={() => {}} placeholder="Ask away" />);
    expect(screen.getByPlaceholderText("Ask away")).toBeInTheDocument();
  });
});
