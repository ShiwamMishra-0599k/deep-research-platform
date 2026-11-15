import { useEffect, useId, useRef } from "react";

interface Props {
  title: string;
  description: string;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
}

const FOCUSABLE = "button:not([disabled]), [href], input, select, textarea, [tabindex]:not([tabindex='-1'])";

/**
 * Modal confirmation. Focus moves to Cancel (the safe choice), Tab is kept inside
 * the dialog, Esc cancels, and focus returns to the trigger on close.
 */
export default function ConfirmDialog({ title, description, confirmLabel, onConfirm, onCancel }: Props) {
  const panelRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const descId = useId();

  useEffect(() => {
    const previouslyFocused = document.activeElement as HTMLElement | null;
    cancelRef.current?.focus();
    return () => previouslyFocused?.focus?.();
  }, []);

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") {
      e.stopPropagation();
      onCancel();
      return;
    }
    if (e.key !== "Tab") return;
    const nodes = panelRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE);
    if (!nodes || nodes.length === 0) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center p-4 bg-ink/40"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onCancel(); }}
      onKeyDown={onKeyDown}
    >
      <div
        ref={panelRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descId}
        className="card w-full max-w-sm p-5 shadow-md"
      >
        <h2 id={titleId} className="text-lg font-semibold">{title}</h2>
        <p id={descId} className="mt-1.5 text-sm text-muted">{description}</p>
        <div className="mt-5 flex justify-end gap-2">
          <button ref={cancelRef} type="button" className="btn-secondary" onClick={onCancel}>Cancel</button>
          <button type="button" className="btn-danger" onClick={onConfirm}>{confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}
