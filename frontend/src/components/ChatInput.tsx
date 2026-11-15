import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import { ArrowUp, Square } from "lucide-react";

interface Props {
  onSend: (text: string) => void;
  onStop?: () => void;
  busy?: boolean;
  placeholder?: string;
}

export default function ChatInput({ onSend, onStop, busy, placeholder }: Props) {
  const [value, setValue] = useState("");
  const taRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (taRef.current) {
      taRef.current.style.height = "auto";
      taRef.current.style.height = Math.min(taRef.current.scrollHeight, 200) + "px";
    }
  }, [value]);

  function submit(e?: FormEvent) {
    e?.preventDefault();
    const v = value.trim();
    if (!v || busy) return;
    onSend(v);
    setValue("");
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    // Skip during IME composition (CJK input, mobile predictive text)
    if ((e.nativeEvent as any).isComposing || e.keyCode === 229) return;
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  return (
    <form onSubmit={submit}>
      {/* The wrapper carries the focus ring so the textarea needs no outline of its own. */}
      <div
        className="flex items-end gap-2 rounded-card bg-surface border border-line-strong p-2 pl-3 transition-colors
                   focus-within:border-accent-strong focus-within:ring-1 focus-within:ring-accent-strong"
      >
        <textarea
          ref={taRef}
          rows={1}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKey}
          aria-label="Ask a research question"
          placeholder={placeholder ?? "Ask about markets, news, places or any topic"}
          className="flex-1 resize-none bg-transparent text-base text-ink placeholder:text-placeholder
                     py-1.5 max-h-52 focus:outline-none focus-visible:outline-none"
        />
        {busy && onStop ? (
          <button
            type="button" onClick={onStop} aria-label="Stop generating" title="Stop"
            className="btn-icon bg-surface border border-line-strong text-ink hover:bg-sunken"
          >
            <Square size={14} className="fill-current" aria-hidden="true" />
          </button>
        ) : (
          <button
            type="submit" disabled={!value.trim() || busy} aria-label="Send" title="Send"
            className="btn-icon bg-accent-strong text-white hover:bg-accent-deep
                       disabled:opacity-40 disabled:pointer-events-none"
          >
            <ArrowUp size={18} aria-hidden="true" />
          </button>
        )}
      </div>
      <p className="meta mt-1.5 px-1 hidden sm:block">Enter to send · Shift+Enter for a new line</p>
    </form>
  );
}
