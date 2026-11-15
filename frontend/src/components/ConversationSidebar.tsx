import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Plus, Trash2, LogOut } from "lucide-react";
import { useChat } from "@/store/chat";
import { useAuth } from "@/store/auth";
import type { Conversation } from "@/types";
import Logo from "./Logo";
import ConfirmDialog from "./ConfirmDialog";

export default function ConversationSidebar({ open, onClose }: { open: boolean; onClose: () => void }) {
  const nav = useNavigate();
  const { id: routeId } = useParams();
  const conversations = useChat((s) => s.conversations);
  const activeId = useChat((s) => s.activeId);
  const load = useChat((s) => s.loadConversations);
  const create = useChat((s) => s.createConversation);
  const select = useChat((s) => s.selectConversation);
  const remove = useChat((s) => s.deleteConversation);
  const user = useAuth((s) => s.user);
  const logout = useAuth((s) => s.logout);
  const [pendingDelete, setPendingDelete] = useState<Conversation | null>(null);

  useEffect(() => { load().catch(() => {}); }, [load]);  // 401 is handled globally (redirect to sign-in)
  useEffect(() => {
    if (routeId && routeId !== activeId) {
      select(routeId).then((ok) => { if (!ok) nav("/", { replace: true }); });
    }
  }, [routeId, activeId, select, nav]);

  async function handleNew() {
    const c = await create();
    nav(`/c/${c.id}`);
    onClose();
  }

  async function confirmDelete() {
    if (!pendingDelete) return;
    const { id } = pendingDelete;
    setPendingDelete(null);
    await remove(id);
    if (activeId === id) nav("/");
  }

  return (
    <>
      {/* Backdrop for the mobile drawer */}
      {open && (
        <button
          type="button"
          aria-label="Close navigation"
          onClick={onClose}
          className="fixed inset-0 z-30 bg-ink/40 lg:hidden"
        />
      )}

      <aside
        id="app-nav"
        aria-label="Sidebar"
        className={`on-nav fixed inset-y-0 left-0 z-40 w-sidebar max-w-[85vw] shrink-0 flex flex-col
                    bg-nav text-nav-text transition-[transform,visibility] duration-200
                    lg:static lg:max-w-none lg:translate-x-0 lg:visible
                    ${open ? "translate-x-0 visible" : "-translate-x-full invisible"}`}
      >
        <div className="h-header shrink-0 px-4 flex items-center gap-2.5 border-b border-nav-line">
          <Logo onDark />
          <span className="text-base font-semibold">Lumen</span>
        </div>

        <div className="p-3">
          <button onClick={handleNew} className="btn-primary w-full">
            <Plus size={16} aria-hidden="true" />
            New chat
          </button>
        </div>

        <nav aria-label="Conversations" className="flex-1 overflow-y-auto px-3 pb-3">
          {conversations.length === 0 ? (
            <p className="px-2 py-6 text-sm text-nav-muted">No conversations yet.</p>
          ) : (
            <>
              <h2 className="px-2 pb-1.5 text-xs font-medium text-nav-muted">Recent</h2>
              <ul className="space-y-0.5">
                {conversations.map((c) => {
                  const active = c.id === activeId;
                  return (
                    <li key={c.id} className="group relative">
                      <button
                        onClick={() => { nav(`/c/${c.id}`); onClose(); }}
                        aria-current={active ? "page" : undefined}
                        className={`w-full h-9 text-left rounded-control pl-3 pr-9 text-sm truncate transition-colors
                          ${active
                            ? "bg-nav-raised text-nav-text shadow-[inset_2px_0_0_rgb(var(--c-accent-on-dark))]"
                            : "text-nav-muted hover:bg-nav-raised hover:text-nav-text"}`}
                      >
                        {c.title}
                      </button>
                      <button
                        onClick={() => setPendingDelete(c)}
                        aria-label={`Delete conversation: ${c.title}`}
                        className="btn-icon absolute right-0.5 top-0 h-9 w-8 text-nav-muted hover:text-danger-on-dark
                                   opacity-0 group-hover:opacity-100 focus-visible:opacity-100
                                   [@media(hover:none)]:opacity-100"
                      >
                        <Trash2 size={14} aria-hidden="true" />
                      </button>
                    </li>
                  );
                })}
              </ul>
            </>
          )}
        </nav>

        <div className="shrink-0 border-t border-nav-line p-3 flex items-center gap-2.5">
          <div
            aria-hidden="true"
            className="h-8 w-8 shrink-0 rounded-full bg-nav-raised grid place-items-center text-sm font-medium"
          >
            {user?.name?.[0]?.toUpperCase() ?? "?"}
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-sm font-medium truncate">{user?.name}</div>
            <div className="text-xs text-nav-muted truncate">{user?.email}</div>
          </div>
          <button
            onClick={() => { logout(); nav("/login"); }}
            aria-label="Sign out"
            title="Sign out"
            className="btn-icon text-nav-muted hover:bg-nav-raised hover:text-nav-text"
          >
            <LogOut size={16} aria-hidden="true" />
          </button>
        </div>
      </aside>

      {pendingDelete && (
        <ConfirmDialog
          title="Delete this conversation?"
          description={`“${pendingDelete.title}” and its messages will be permanently removed.`}
          confirmLabel="Delete"
          onConfirm={confirmDelete}
          onCancel={() => setPendingDelete(null)}
        />
      )}
    </>
  );
}
