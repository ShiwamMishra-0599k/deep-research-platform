import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ConfirmDialog from "@/components/ConfirmDialog";

function setup() {
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  render(<ConfirmDialog title="Delete this conversation?" description="It will be removed." confirmLabel="Delete" onConfirm={onConfirm} onCancel={onCancel} />);
  return { onConfirm, onCancel };
}

describe("ConfirmDialog", () => {
  it("is an accessible modal alertdialog labelled by its title and described by its text", () => {
    setup();
    const dlg = screen.getByRole("alertdialog", { name: "Delete this conversation?" });
    expect(dlg).toHaveAttribute("aria-modal", "true");
    expect(dlg).toHaveAccessibleDescription("It will be removed.");
  });

  it("puts initial focus on Cancel (the safe choice), not the destructive button", () => {
    setup();
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
  });

  it("traps Tab: from the last control it wraps to the first, and Shift+Tab wraps back", async () => {
    setup();
    const cancel = screen.getByRole("button", { name: "Cancel" });
    const del = screen.getByRole("button", { name: "Delete" });
    await userEvent.tab();
    expect(del).toHaveFocus();
    await userEvent.tab();
    expect(cancel).toHaveFocus(); // wrapped, did not escape to the page
    await userEvent.tab({ shift: true });
    expect(del).toHaveFocus(); // wrapped backwards
  });

  it("Escape cancels", async () => {
    const { onCancel, onConfirm } = setup();
    await userEvent.keyboard("{Escape}");
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("Cancel cancels, Delete confirms (and only that)", async () => {
    const { onCancel, onConfirm } = setup();
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onCancel).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("clicking the backdrop cancels, but clicking inside the panel does not", () => {
    const { onCancel } = setup();
    const dlg = screen.getByRole("alertdialog");
    fireEvent.mouseDown(dlg);
    expect(onCancel).not.toHaveBeenCalled();
    fireEvent.mouseDown(dlg.parentElement!);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("returns focus to the element that opened it when it closes", async () => {
    function Host() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button onClick={() => setOpen(true)}>open</button>
          {open && <ConfirmDialog title="T" description="D" confirmLabel="Go" onConfirm={() => {}} onCancel={() => setOpen(false)} />}
        </>
      );
    }
    render(<Host />);
    const trigger = screen.getByRole("button", { name: "open" });
    await userEvent.click(trigger);
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });
});
