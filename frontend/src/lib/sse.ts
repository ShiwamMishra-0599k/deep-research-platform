import { fetchEventSource } from "@microsoft/fetch-event-source";

export interface SseEvent {
  event?: string;
  data: string;
  id?: string;
  retry?: number;
}

export type SseHandlers = {
  onEvent: (msg: SseEvent) => void;
  onError?: (err: unknown) => void;
  onClose?: () => void;
  signal?: AbortSignal;
};

/** The server answered the stream request with a non-2xx status. */
export class SseHttpError extends Error {
  constructor(public status: number) {
    super(`HTTP ${status}`);
  }
}

const baseURL = import.meta.env.VITE_API_URL || "/api";

export async function postSse(path: string, body: unknown, handlers: SseHandlers) {
  const token = localStorage.getItem("auth_token");
  await fetchEventSource(`${baseURL}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
    signal: handlers.signal,
    openWhenHidden: true,
    async onopen(res) {
      if (res.ok) return;
      // Same handling as the axios interceptor: an expired/invalid session goes back to sign-in.
      if (res.status === 401) {
        localStorage.removeItem("auth_token");
        localStorage.removeItem("auth_user");
        if (!location.pathname.startsWith("/login") && !location.pathname.startsWith("/register")) {
          location.href = "/login";
        }
      }
      throw new SseHttpError(res.status);
    },
    onmessage: handlers.onEvent,
    onerror(err) {
      handlers.onError?.(err);
      throw err;  // stop retrying
    },
    onclose() {
      handlers.onClose?.();
    },
  });
}
