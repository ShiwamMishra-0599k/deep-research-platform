import { fetchEventSource } from "@microsoft/fetch-event-source";
import { authHeaders, baseURL, crossOrigin, endSession, refreshSession } from "@/lib/api";

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

/** Internal: the session was refreshed, so the stream request must be opened again (once). */
class ReopenStream extends Error {}

async function attempt(path: string, body: unknown, handlers: SseHandlers, retried: boolean): Promise<void> {
  await fetchEventSource(`${baseURL}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      ...authHeaders("POST"), // X-Auth-Mode + X-CSRF-Token (this is a state-changing POST); never Authorization
    },
    ...(crossOrigin ? { credentials: "include" as const } : {}),
    body: JSON.stringify(body),
    signal: handlers.signal,
    openWhenHidden: true,
    async onopen(res) {
      if (res.ok) return;
      if (res.status === 401) {
        if (!retried) {
          const r = await refreshSession(); // shared with every other in-flight 401
          if (r === "ok") throw new ReopenStream();
          if (r === "ended") endSession();
        } else {
          endSession(); // refreshed once already and still unauthorised
        }
      }
      throw new SseHttpError(res.status);
    },
    onmessage: handlers.onEvent,
    onerror(err) {
      if (!(err instanceof ReopenStream)) handlers.onError?.(err);
      throw err;  // stop retrying
    },
    onclose() {
      handlers.onClose?.();
    },
  });
}

export async function postSse(path: string, body: unknown, handlers: SseHandlers) {
  try {
    await attempt(path, body, handlers, false);
  } catch (e) {
    if (!(e instanceof ReopenStream)) throw e;
    await attempt(path, body, handlers, true);
  }
}
