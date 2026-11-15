import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import MessageBubble from "@/components/MessageBubble";
import CheckpointTrail from "@/components/CheckpointTrail";
import type { Checkpoint, Message } from "@/types";

const base = (over: Partial<Message>): Message => ({ id: "m", role: "assistant", content: "", createdAt: "t", ...over });

describe("MessageBubble: user", () => {
  it("shows the user's text verbatim (line breaks preserved) with no assistant chrome", () => {
    render(<MessageBubble message={base({ role: "user", content: "line 1" + String.fromCharCode(10) + "line 2" })} />);
    const p = screen.getByText(/line 1/);
    expect(p.textContent).toBe("line 1" + String.fromCharCode(10) + "line 2");
    expect(p.className).toContain("whitespace-pre-wrap");
    expect(screen.queryByText("Lumen")).not.toBeInTheDocument();
  });

  it("renders user text as text, not HTML", () => {
    const { container } = render(<MessageBubble message={base({ role: "user", content: "<b>x</b><img src=x>" })} />);
    expect(container.querySelector("b")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("<b>x</b><img src=x>")).toBeInTheDocument();
  });
});

describe("MessageBubble: assistant", () => {
  it("renders blocks when present and is labelled as a Lumen response", () => {
    render(<MessageBubble message={base({ blocks: [{ template_id: "markdown", data: { content: "The answer" } }] })} />);
    expect(screen.getByRole("article", { name: "Lumen response" })).toBeInTheDocument();
    expect(screen.getByText("The answer")).toBeInTheDocument();
  });

  it("falls back to the plain content as markdown when there are no blocks", () => {
    render(<MessageBubble message={base({ content: "**plain** answer" })} />);
    expect(screen.getByText("plain").tagName).toBe("STRONG");
  });

  it("while streaming with nothing yet: shows a status skeleton and aria-busy; not busy when finished", () => {
    const { rerender } = render(<MessageBubble message={base({})} streaming />);
    expect(screen.getByRole("status")).toHaveTextContent(/working on your question/i);
    expect(screen.getByRole("article")).toHaveAttribute("aria-busy", "true");
    rerender(<MessageBubble message={base({})} />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.getByRole("article")).not.toHaveAttribute("aria-busy");
  });

  it("shows the live trail only when checkpoints are supplied", () => {
    const cps: Checkpoint[] = [{ status: "in_progress", content: "Searching the web", ts: 1000 }];
    const { rerender } = render(<MessageBubble message={base({})} streaming checkpoints={cps} />);
    expect(screen.getByRole("region", { name: "Research progress" })).toHaveTextContent("Searching the web");
    rerender(<MessageBubble message={base({})} streaming checkpoints={[]} />);
    expect(screen.queryByRole("region", { name: "Research progress" })).not.toBeInTheDocument();
  });

  it("sources: shows hostnames without www, caps at 8, opens safely, and tells screen readers it opens a new tab", () => {
    const sources = Array.from({ length: 10 }, (_, i) => `https://www.site${i}.example/path`);
    render(<MessageBubble message={base({ content: "x", sources })} />);
    const links = within(screen.getByRole("region", { name: "Sources" })).getAllByRole("link");
    expect(links).toHaveLength(8);
    expect(links[0]).toHaveTextContent("site0.example");
    expect(links[0]).not.toHaveTextContent("www.");
    expect(links[0]).toHaveAttribute("href", sources[0]);
    expect(links[0]).toHaveAttribute("target", "_blank");
    expect(links[0]).toHaveAttribute("rel", "noreferrer");
    expect(links[0]).toHaveTextContent("(opens in a new tab)");
  });

  it("a malformed source URL is shown as-is rather than crashing", () => {
    render(<MessageBubble message={base({ content: "x", sources: ["not a url"] })} />);
    expect(screen.getByRole("link", { name: /not a url/ })).toBeInTheDocument();
  });

  it("no sources section when there are none", () => {
    render(<MessageBubble message={base({ content: "x", sources: [] })} />);
    expect(screen.queryByRole("region", { name: "Sources" })).not.toBeInTheDocument();
  });

  it("follow-ups: each button calls back with its own suggestion; hidden without a handler", async () => {
    const onFollowUp = vi.fn();
    const followUps = [{ label: "Compare", query: "Compare A and B" }, { label: "Why?", query: "Why did it move?" }];
    const { rerender } = render(<MessageBubble message={base({ content: "x", followUps })} onFollowUp={onFollowUp} />);
    await userEvent.click(screen.getByRole("button", { name: "Why?" }));
    expect(onFollowUp).toHaveBeenCalledExactlyOnceWith(followUps[1]);
    rerender(<MessageBubble message={base({ content: "x", followUps })} />);
    expect(screen.queryByRole("region", { name: /follow-ups/i })).not.toBeInTheDocument();
  });
});

describe("CheckpointTrail", () => {
  const cp = (content: string, ts: number): Checkpoint => ({ status: "in_progress", content, ts });

  it("renders nothing for an empty list", () => {
    const { container } = render(<CheckpointTrail items={[]} active />);
    expect(container).toBeEmptyDOMElement();
  });

  it("while active: title 'Researching', step count, and elapsed times relative to the first step", () => {
    render(<CheckpointTrail active items={[cp("one", 1000), cp("two", 1500), cp("three", 66_000)]} />);
    const trail = screen.getByRole("region", { name: "Research progress" });
    expect(trail).toHaveTextContent("Researching");
    expect(trail).toHaveTextContent("3 steps");
    const times = within(trail).getAllByText(/^\+/).map((e) => e.textContent);
    expect(times).toEqual(["+0ms", "+500ms", "+1m 5s"]);
  });

  it("formats seconds with one decimal", () => {
    render(<CheckpointTrail active items={[cp("a", 0), cp("b", 2300)]} />);
    expect(screen.getByText("+2.3s")).toBeInTheDocument();
  });

  it("singular '1 step' wording", () => {
    render(<CheckpointTrail active items={[cp("only", 5)]} />);
    expect(screen.getByRole("region")).toHaveTextContent("1 step");
    expect(screen.getByRole("region")).not.toHaveTextContent("1 steps");
  });

  it("when finished the title changes and no step is marked in progress", () => {
    render(<CheckpointTrail active={false} items={[cp("a", 0), cp("b", 10)]} />);
    expect(screen.getByRole("region")).toHaveTextContent("Research steps");
  });

  it("is a polite live region so screen readers hear new steps", () => {
    render(<CheckpointTrail active items={[cp("a", 0)]} />);
    expect(screen.getByRole("region")).toHaveAttribute("aria-live", "polite");
  });
});
