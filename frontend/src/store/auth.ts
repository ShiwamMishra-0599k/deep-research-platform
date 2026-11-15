import { create } from "zustand";
import { api } from "@/lib/api";
import type { User } from "@/types";
import { useChat } from "@/store/chat";

interface AuthState {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string, name: string) => Promise<void>;
  logout: () => void;
  hydrate: () => void;
}

function readStoredUser(): User | null {
  try {
    const raw = localStorage.getItem("auth_user");
    return raw ? (JSON.parse(raw) as User) : null;
  } catch {
    return null;
  }
}

export const useAuth = create<AuthState>((set) => ({
  user: readStoredUser(),
  loading: false,

  hydrate: () => {
    const raw = localStorage.getItem("auth_user");
    if (raw) {
      try { set({ user: JSON.parse(raw) }); } catch {}
    }
  },

  login: async (email, password) => {
    set({ loading: true });
    try {
      const { data } = await api.post("/auth/login", { email, password });
      localStorage.setItem("auth_token", data.token);
      const u = { id: data.id, email: data.email, name: data.name };
      localStorage.setItem("auth_user", JSON.stringify(u));
      set({ user: u });
    } finally {
      set({ loading: false });
    }
  },

  register: async (email, password, name) => {
    set({ loading: true });
    try {
      const { data } = await api.post("/auth/register", { email, password, name });
      localStorage.setItem("auth_token", data.token);
      const u = { id: data.id, email: data.email, name: data.name };
      localStorage.setItem("auth_user", JSON.stringify(u));
      set({ user: u });
    } finally {
      set({ loading: false });
    }
  },

  logout: () => {
    localStorage.removeItem("auth_token");
    localStorage.removeItem("auth_user");
    // The previous person's conversations and messages must not survive in memory:
    // the next person to sign in on this tab would otherwise see them.
    useChat.setState({ conversations: [], activeId: null, messages: {}, loading: false });
    set({ user: null });
  },
}));
