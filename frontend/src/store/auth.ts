import { create } from "zustand";
import { api, onSessionEnded, refreshSession } from "@/lib/api";
import type { User } from "@/types";
import { useChat } from "@/store/chat";

export type AuthStatus = "loading" | "authenticated" | "anonymous";

interface AuthState {
  user: User | null;
  /** loading until the first /auth/me (or legacy upgrade) answers; Protected routes wait on it (no login flash). */
  status: AuthStatus;
  loading: boolean; // a login/register request is in flight
  booted: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string, name: string) => Promise<void>;
  logout: () => void;
  hydrate: () => Promise<void>;
}

const toUser = (d: { id: string; email: string; name: string }): User => ({ id: d.id, email: d.email, name: d.name });

/**
 * GET /auth/me, tolerating an expired ACCESS cookie. The access cookie lives 15 minutes, the refresh session up to
 * 24 h idle: a 401 here usually means "refresh me", not "signed out". Without this, reopening the app after a coffee
 * break would force a fresh login and the 24 h inactivity window would be meaningless. One refresh, one retry, no loop.
 */
async function fetchMe(): Promise<{ id: string; email: string; name: string }> {
  try {
    return (await api.get("/auth/me")).data;
  } catch (e: any) {
    if (e?.response?.status !== 401) throw e;
    if ((await refreshSession()) !== "ok") throw e;
    return (await api.get("/auth/me")).data;
  }
}

/** Nothing about the previous person may survive in memory: the next one to sign in on this tab would see it. */
function resetLocalSession(set: (p: Partial<AuthState>) => void) {
  useChat.setState({ conversations: [], activeId: null, messages: {}, loading: false });
  set({ user: null, status: "anonymous" });
}

// Other tabs: a logout in one tab must not leave the others looking signed in.
const CHANNEL = "lumen-auth";
let channel: BroadcastChannel | null = null;

export const useAuth = create<AuthState>((set, get) => {
  function ensureChannel(): BroadcastChannel | null {
    if (channel || typeof BroadcastChannel === "undefined") return channel;
    channel = new BroadcastChannel(CHANNEL);
    channel.onmessage = (e: MessageEvent) => {
      // Reset local state only: no server call and no re-broadcast, so tabs cannot ping-pong.
      if (e?.data?.type === "logout") resetLocalSession(set);
    };
    return channel;
  }

  // The API layer reports that the server ended the session (refresh failed with 401/403).
  onSessionEnded(() => resetLocalSession(set));

  return {
    user: null,
    status: "loading",
    loading: false,
    booted: false,

    hydrate: async () => {
      if (get().booted) return; // StrictMode runs effects twice; the boot must happen once
      set({ booted: true });
      ensureChannel();

      // 1. Pre-migration sessions: upgrade a stored Bearer token into cookies once, then forget it.
      let legacy: string | null = null;
      try { legacy = localStorage.getItem("auth_token"); } catch { /* storage unavailable */ }
      try {
        if (legacy) {
          try {
            const { data } = await api.post("/auth/session/upgrade", undefined, { headers: { Authorization: `Bearer ${legacy}` } });
            set({ user: toUser(data), status: "authenticated" });
            return;
          } catch (e: any) {
            // 400 = a cookie session already exists (another tab upgraded first): fall through to /me.
            // Anything else (401 expired, 404 backend no longer dual, network): signed out.
            if (e?.response?.status !== 400) {
              set({ user: null, status: "anonymous" });
              return;
            }
          }
        }
        // 2. Normal boot: the HttpOnly cookie is sent automatically; the server decides.
        const data = await fetchMe();
        set({ user: toUser(data), status: "authenticated" });
      } catch (e: any) {
        set({ user: null, status: "anonymous" });
        if (!e?.response) set({ booted: false }); // network error: a later hydrate() may try again
      } finally {
        // Unconditional: no pre-migration credential or identity may remain readable by script.
        try {
          localStorage.removeItem("auth_token");
          localStorage.removeItem("auth_user");
        } catch { /* storage unavailable */ }
      }
    },

    login: async (email, password) => {
      set({ loading: true });
      try {
        const { data } = await api.post("/auth/login", { email, password });
        set({ user: toUser(data), status: "authenticated" });
      } finally {
        set({ loading: false });
      }
    },

    register: async (email, password, name) => {
      set({ loading: true });
      try {
        const { data } = await api.post("/auth/register", { email, password, name });
        set({ user: toUser(data), status: "authenticated" });
      } finally {
        set({ loading: false });
      }
    },

    logout: () => {
      // Best effort: the local sign-out must happen even if the server call fails (offline, expired).
      api.post("/auth/logout").catch(() => {});
      try {
        localStorage.removeItem("auth_token");
        localStorage.removeItem("auth_user");
      } catch { /* storage unavailable */ }
      resetLocalSession(set);
      ensureChannel()?.postMessage({ type: "logout" });
    },
  };
});
