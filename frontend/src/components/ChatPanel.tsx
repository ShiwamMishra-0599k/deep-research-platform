import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Menu, TrendingUp, Newspaper, MapPin, Bitcoin, DollarSign } from "lucide-react";
import { useAuth } from "@/store/auth";
import { useChat } from "@/store/chat";
import { postSse } from "@/lib/sse";
import type { Block, Checkpoint, FollowUp, Message } from "@/types";
import MessageBubble from "./MessageBubble";
import ChatInput from "./ChatInput";

const SUGGESTIONS: { label: string; icon: JSX.Element }[] = [
  { label: "EV charging stations in Lucknow",       icon: <MapPin size={16} /> },
  { label: "Latest RBI policy update",              icon: <Newspaper size={16} /> },
  { label: "Bitcoin price in INR",                  icon: <Bitcoin size={16} /> },
  { label: "Top IT stocks on NSE today",            icon: <TrendingUp size={16} /> },
  { label: "Hospitals near Koramangala Bangalore",  icon: <MapPin size={16} /> },
  { label: "1000 USD to INR",                       icon: <DollarSign size={16} /> },
];

function timeGreeting(): string {
  const h = new Date().getHours();
  if (h < 12) return "Good morning";
  if (h < 17) return "Good afternoon";
  return "Good evening";
}

export default function ChatPanel({ onOpenNav }: { onOpenNav: () => void }) {
  const nav = useNavigate();
  const activeId = useChat((s) => s.activeId);
  const allMsgs = useChat((s) => s.messages);
  const addMessage = useChat((s) => s.addMessage);
  const updateMessage = useChat((s) => s.updateMessage);
  const createConversation = useChat((s) => s.createConversation);
  const renameConversation = useChat((s) => s.renameConversation);
  const select = useChat((s) => s.selectConversation);

  const messages = useMemo(() => (activeId ? allMsgs[activeId] || [] : []), [activeId, allMsgs]);
  const title = useChat((s) => s.conversations.find((c) => c.id === s.activeId)?.title);

  const [streaming, setStreaming] = useState(false);
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [pendingAssistantId, setPendingAssistantId] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);

  // Track whether the user has scrolled up — if so, don't yank them back.
  function onScroll() {
    const el = scrollRef.current;
    if (!el) return;
    const distFromBottom = el.scrollHeight - el.clientHeight - el.scrollTop;
    stickToBottomRef.current = distFromBottom < 80;
  }

  useEffect(() => {
    if (!stickToBottomRef.current) return;
    const el = scrollRef.current;
    if (!el) return;
    // 'auto' (instant) during stream so rapid checkpoints don't jitter; 'smooth' otherwise.
    el.scrollTo({ top: el.scrollHeight, behavior: streaming ? "auto" : "smooth" });
  }, [messages.length, checkpoints.length, streaming]);

  async function send(text: string) {
    let convId = activeId;
    if (!convId) {
      const c = await createConversation();
      convId = c.id;
      nav(`/c/${convId}`);
      await select(convId);
    }
    if (!convId) return;

    // Optimistic user + assistant placeholder
    const tempUser: Message = {
      id: `tmp-user-${Date.now()}`,
      role: "user",
      content: text,
      createdAt: new Date().toISOString(),
    };
    const tempAssistant: Message = {
      id: `tmp-asst-${Date.now()}`,
      role: "assistant",
      content: "",
      blocks: [],
      sources: [],
      followUps: [],
      createdAt: new Date().toISOString(),
    };
    addMessage(convId, tempUser);
    addMessage(convId, tempAssistant);
    setPendingAssistantId(tempAssistant.id);
    setCheckpoints([]);
    setStreaming(true);
    stickToBottomRef.current = true;  // a new turn — follow it down

    if (messages.length === 0) {
      const newTitle = text.length > 60 ? text.slice(0, 57) + "..." : text;
      renameConversation(convId, newTitle).catch(() => {});
    }

    const ctrl = new AbortController();
    abortRef.current = ctrl;

    let collectedBlocks: Block[] = [];
    let collectedSources: string[] = [];
    let collectedFollowUps: FollowUp[] = [];
    let collectedText = "";

    try {
      await postSse(`/conversations/${convId}/query`, { query: text }, {
        signal: ctrl.signal,
        onEvent: (msg) => {
          if (!msg.event) return;
          let data: any = {};
          try { data = JSON.parse(msg.data); } catch {}

          switch (msg.event) {
            case "user_message":
              if (data.id) {
                updateMessage(convId!, tempUser.id, { id: data.id });
              }
              break;
            case "checkpoint":
              setCheckpoints((cs) => [
                ...cs,
                { status: data.status, content: data.content, tool: data.tool, ts: Date.now() },
              ]);
              break;
            case "blocks": {
              const payload = data.data || {};
              collectedBlocks = payload.blocks || [];
              collectedSources = payload.sources || [];
              collectedFollowUps = payload.follow_ups || [];
              const md = collectedBlocks.find((b) => b.template_id === "markdown") as any;
              collectedText = md?.data?.content || "";
              updateMessage(convId!, tempAssistant.id, {
                content: collectedText,
                blocks: collectedBlocks,
                sources: collectedSources,
                followUps: collectedFollowUps,
              });
              break;
            }
            case "clarification":
              collectedText = data.content || "";
              updateMessage(convId!, tempAssistant.id, {
                content: collectedText,
                blocks: [{ template_id: "markdown", data: { content: collectedText } }],
              });
              break;
            case "error":
              collectedText = data.content || "Something went wrong.";
              updateMessage(convId!, tempAssistant.id, {
                content: collectedText,
                blocks: [{ template_id: "markdown", data: { content: `**Error:** ${collectedText}` } }],
              });
              break;
            case "persisted":
              if (data.messageId) {
                updateMessage(convId!, tempAssistant.id, { id: data.messageId });
              }
              break;
          }
        },
        onError: (e) => {
          console.error("SSE error", e);
          updateMessage(convId!, tempAssistant.id, {
            content: "Connection lost. Please retry.",
            blocks: [{ template_id: "markdown", data: { content: "**Connection lost.** Please retry." } }],
          });
        },
      });
    } catch (e) {
      // already handled in onError
    } finally {
      setStreaming(false);
      setPendingAssistantId(null);
      setCheckpoints([]);
      abortRef.current = null;
    }
  }

  function stop() {
    abortRef.current?.abort();
    setStreaming(false);
    if (pendingAssistantId && activeId) {
      updateMessage(activeId, pendingAssistantId, {
        content: "Stopped.",
        blocks: [{ template_id: "markdown", data: { content: "_Stopped by user._" } }],
      });
    }
    setPendingAssistantId(null);
  }

  function handleFollowUp(f: FollowUp) {
    send(f.query);
  }

  return (
    <main id="main" className="flex-1 min-w-0 h-full flex flex-col">
      <header className="h-header shrink-0 px-4 sm:px-6 flex items-center gap-2 border-b border-line">
        <button
          type="button"
          onClick={onOpenNav}
          aria-label="Open navigation"
          aria-controls="app-nav"
          className="btn-icon -ml-2 text-muted hover:bg-sunken hover:text-ink lg:hidden"
        >
          <Menu size={20} aria-hidden="true" />
        </button>
        <h1 className="text-sm font-medium truncate">{messages.length ? title ?? "Conversation" : "New research"}</h1>
      </header>

      <div
        ref={scrollRef}
        onScroll={onScroll}
        role="log"
        aria-label="Conversation"
        className="relative flex-1 overflow-y-auto overscroll-contain"
      >
        <div className="mx-auto w-full max-w-content px-4 sm:px-6 py-6 min-h-full flex flex-col">
          {messages.length === 0 ? (
            <Welcome onPick={(s) => send(s)} />
          ) : (
            <div className="space-y-8">
              {messages.map((m) => (
                <MessageBubble
                  key={m.id}
                  message={m}
                  streaming={streaming && m.id === pendingAssistantId}
                  checkpoints={m.id === pendingAssistantId ? checkpoints : undefined}
                  onFollowUp={handleFollowUp}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="shrink-0 border-t border-line bg-canvas">
        <div className="mx-auto w-full max-w-content px-4 sm:px-6 py-3">
          <ChatInput onSend={send} onStop={stop} busy={streaming} />
        </div>
      </div>
    </main>
  );
}

function Welcome({ onPick }: { onPick: (s: string) => void }) {
  const user = useAuth((s) => s.user);
  const firstName = user?.name?.split(" ")[0] ?? "";
  return (
    <section aria-labelledby="welcome-title" className="my-auto py-8">
      <p className="text-sm text-muted">{timeGreeting()}{firstName ? `, ${firstName}` : ""}</p>
      <h2 id="welcome-title" className="mt-1 text-xl font-semibold">What would you like to research?</h2>
      <p className="mt-2 text-sm text-muted max-w-prose">
        Ask about markets, news, places or any topic. Lumen searches live sources and cites what it finds.
      </p>

      <h3 className="mt-8 mb-2 text-xs font-medium text-muted">Try asking</h3>
      <ul className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        {SUGGESTIONS.map((s) => (
          <li key={s.label}>
            <button
              onClick={() => onPick(s.label)}
              className="card w-full px-3 py-2.5 text-sm text-left flex items-center gap-2.5
                         hover:bg-sunken hover:border-line-strong transition-colors"
            >
              <span className="shrink-0 text-accent-strong" aria-hidden="true">{s.icon}</span>
              <span className="truncate">{s.label}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
