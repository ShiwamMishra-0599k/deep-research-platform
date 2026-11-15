import { useEffect, useState } from "react";
import ConversationSidebar from "@/components/ConversationSidebar";
import ChatPanel from "@/components/ChatPanel";

export default function Chat() {
  // Below `lg` the sidebar is an off-canvas drawer; at `lg`+ it is always visible.
  const [navOpen, setNavOpen] = useState(false);

  useEffect(() => {
    if (!navOpen) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setNavOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navOpen]);

  return (
    <div className="relative h-dvh w-full flex overflow-hidden bg-canvas">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:z-50 focus:m-2 focus:btn-primary"
      >
        Skip to conversation
      </a>
      <ConversationSidebar open={navOpen} onClose={() => setNavOpen(false)} />
      <ChatPanel onOpenNav={() => setNavOpen(true)} />
    </div>
  );
}
