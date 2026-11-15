"""Shutdown awareness for in-flight research streams.

When the process receives SIGTERM (a deploy, a scale-in, `docker stop`) uvicorn stops accepting connections and waits for open streams, up to
`--timeout-graceful-shutdown` seconds (set from SHUTDOWN_GRACE_SECONDS in the Dockerfile, deliberately BELOW the orchestrator's stop timeout so the
process exits by itself instead of being killed). Streams still open after that are cancelled.

This module lets the research route tell "cancelled because the service is stopping" apart from "the user closed the tab":
* in the first case it saves a short "interrupted, please ask again" answer so the conversation is never left with a question and silence;
* new research requests are refused immediately with 503 + Retry-After while the service is stopping (a keep-alive connection could still send one).

The flag is set by a hook CHAINED onto the signal handlers uvicorn installed, so uvicorn's own shutdown behaviour is unchanged.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import threading
import time

log = logging.getLogger("lumen.shutdown")

_flag = threading.Event()
_installed = False
_lock = threading.Lock()
_inflight = 0

INTERRUPTED_TEXT = "The service was restarting while it was answering. Please ask again."


def is_shutting_down() -> bool:
    return _flag.is_set()


def stream_started() -> None:
    global _inflight
    with _lock:
        _inflight += 1


def stream_finished() -> None:
    global _inflight
    with _lock:
        _inflight = max(0, _inflight - 1)


def streams_in_flight() -> int:
    return _inflight


async def wait_for_streams(timeout: float) -> bool:
    """Wait until every research stream has finished unwinding (including saving its note). uvicorn cancels overdue streams but does not wait
    for them, so the lifespan shutdown must, or they create new worker threads after those were stopped. Returns False on timeout."""
    deadline = time.monotonic() + timeout
    while _inflight > 0:
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


def begin() -> None:
    _flag.set()


def reset() -> None:
    """Tests only."""
    _flag.clear()


def install_signal_hook() -> None:
    """Call once at start-up, AFTER the server installed its handlers (the lifespan start does). Safe to call from any thread: it no-ops off the main thread."""
    global _installed
    if _installed or threading.current_thread() is not threading.main_thread():
        return
    _installed = True
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous = signal.getsignal(sig)

            def hook(signum, frame, _previous=previous):
                _flag.set()
                if callable(_previous):
                    _previous(signum, frame)

            signal.signal(sig, hook)
        except (ValueError, OSError):   # unsupported on this platform or context: shutdown still works, only the interrupted note is lost
            log.debug("could not hook %s", sig)


def stop_worker_threads() -> int:
    """Ask every anyio worker thread (the pool behind `run_in_threadpool`) to exit; returns how many were asked.

    Why: after uvicorn cancels streams that outlive the graceful-shutdown timeout, idle worker threads can be left running. They are NOT daemon
    threads, so the interpreter waits for them forever and the container is only ended by SIGKILL (measured: exit 137 after the full stop timeout,
    with the main thread parked in `threading._shutdown` and four idle `AnyIO worker thread`s). Called at the very end of the lifespan, when no
    request can still need them. Feature-detected: if anyio's internals change this does nothing and shutdown behaves as before.
    """
    try:
        from anyio._backends._asyncio import WorkerThread
    except Exception:   # noqa: BLE001
        return 0
    asked = 0
    for t in threading.enumerate():
        if isinstance(t, WorkerThread):
            try:
                t.stop()
                asked += 1
            except Exception:   # noqa: BLE001
                log.debug("could not stop worker thread", exc_info=True)
    return asked
