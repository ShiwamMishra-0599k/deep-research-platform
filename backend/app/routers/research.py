"""SSE bridge: persists user/assistant messages and streams agent events to the client."""
import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from ..config import settings
from ..db import SessionLocal
from ..deps import get_current_user
from ..models import Conversation, Message, Role, User
from ..ratelimit import ConcurrencyGuard, SlidingWindowLimiter
from ..research_client import AgentProtocolError, AgentUnavailable, stream_research
from ..schemas import QueryRequest

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/conversations", tags=["research"])

GENERIC_ERROR = "The research service hit an error. Please try again."
UNAVAILABLE_ERROR = "The research service is currently unavailable. Please try again shortly."
TIMEOUT_ERROR = "The research took too long and was stopped. Try a narrower question."
INTERRUPTED_ERROR = "The research was interrupted before it finished. Please try again."
TOO_LARGE_ERROR = "The research result was too large to display."

# Events the agent may send; anything else is dropped (and logged), never forwarded.
_ALLOWED_EVENTS = {"checkpoint", "blocks", "clarification", "error", "done"}

query_limiter = SlidingWindowLimiter(settings.queries_per_user_per_minute, 60)
stream_guard = ConcurrencyGuard(
    settings.max_concurrent_streams_per_user, ttl=settings.research_timeout_seconds + 120
)


def _sse(event: str, data: str) -> bytes:
    """Format one SSE frame. `data` is JSON text; newlines are split across data: lines."""
    payload = "".join(f"data: {line}\n" for line in data.split("\n"))
    return f"event: {event}\n{payload}\n".encode()


def _err(message: str) -> bytes:
    return _sse("error", json.dumps({"status": "failed", "content": message}))


@dataclass
class _Turn:
    conv_id: uuid.UUID
    user_msg_id: uuid.UUID
    user_msg_created: str
    history: list[dict[str, str]]


@dataclass
class _Result:
    text: str = ""
    blocks: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    follow_ups: list = field(default_factory=list)
    got_terminal: bool = False  # blocks | clarification received
    persisted: bool = False


def _start_turn(user_id: uuid.UUID, conv_id: uuid.UUID, query: str) -> _Turn | None:
    """Ownership check + persist the user's message + load prior history (one transaction)."""
    with SessionLocal() as db:
        conv = (
            db.query(Conversation)
            .filter(Conversation.id == conv_id, Conversation.user_id == user_id)
            .one_or_none()
        )
        if conv is None:
            return None
        history = [
            {"role": m.role.lower(), "content": m.content or ""}
            for m in db.query(Message)
            .filter(Message.conversation_id == conv.id)
            .order_by(Message.created_at.asc())
            .all()
        ]
        user_msg = Message(conversation_id=conv.id, role=Role.USER.value, content=query)
        db.add(user_msg)
        if conv.title == "New chat":
            conv.title = query[:57] + "..." if len(query) > 60 else query
        conv.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(user_msg)
        return _Turn(conv.id, user_msg.id, user_msg.created_at.isoformat(), history)


def _persist_assistant(conv_id: uuid.UUID, res: _Result, error_text: str | None) -> uuid.UUID:
    with SessionLocal() as db:
        ok = error_text is None
        msg = Message(
            conversation_id=conv_id,
            role=Role.ASSISTANT.value,
            content=res.text if ok else error_text,
            blocks=(res.blocks or None) if ok else None,
            sources=(res.sources or None) if ok else None,
            follow_ups=(res.follow_ups or None) if ok else None,
        )
        db.add(msg)
        conv = db.get(Conversation, conv_id)
        if conv is not None:
            conv.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(msg)
        return msg.id


def _absorb(name: str, node: dict, res: _Result) -> str | None:
    """Update `res` from a validated agent event. Returns an error string if the event is unusable."""
    if name == "blocks":
        data = node.get("data")
        blocks = data.get("blocks") if isinstance(data, dict) else None
        if not isinstance(blocks, list) or not all(
            isinstance(b, dict) and isinstance(b.get("template_id"), str) for b in blocks
        ):
            return GENERIC_ERROR
        if len(json.dumps(blocks)) > settings.max_persisted_blocks_bytes:
            return TOO_LARGE_ERROR
        res.blocks = blocks
        for b in blocks:
            if b.get("template_id") == "markdown":
                res.text = str((b.get("data") or {}).get("content", "") or "")
                break
        sources = data.get("sources")
        res.sources = [s for s in sources if isinstance(s, str)] if isinstance(sources, list) else []
        fups = data.get("follow_ups")
        res.follow_ups = [f for f in fups if isinstance(f, dict)] if isinstance(fups, list) else []
        res.got_terminal = True
    elif name == "clarification":
        res.text = str(node.get("content", "") or "")
        res.got_terminal = True
    return None


async def _persist_with_retry(
    conv_id: uuid.UUID, res: _Result, error_text: str | None
) -> uuid.UUID | None:
    for attempt in range(3):
        try:
            return await run_in_threadpool(_persist_assistant, conv_id, res, error_text)
        except Exception:
            log.exception("persist attempt %d failed", attempt + 1)
            await asyncio.sleep(0.2 * (attempt + 1))
    return None


@router.post("/{conv_id}/query")
async def query_conversation(
    conv_id: uuid.UUID,
    req: QueryRequest,
    request: Request,
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    user_id = user.id
    key = str(user_id)

    if not query_limiter.allow(key):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "You are sending requests too quickly. Please wait a moment.",
            headers={"Retry-After": str(query_limiter.retry_after(key))},
        )
    if not stream_guard.acquire(key):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many research requests are already running. Wait for one to finish.",
            headers={"Retry-After": "5"},
        )

    try:
        turn = await run_in_threadpool(_start_turn, user_id, conv_id, req.query)
    except Exception:
        stream_guard.release(key)
        log.exception("failed to persist user message")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Service temporarily unavailable") from None
    if turn is None:
        stream_guard.release(key)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")

    query_text = req.query

    async def generator() -> AsyncIterator[bytes]:
        res = _Result()
        error_text: str | None = None
        agen = stream_research(query_text, turn.history)
        cancelled = False
        try:
            yield _sse(
                "user_message",
                json.dumps({"id": str(turn.user_msg_id), "createdAt": turn.user_msg_created}),
            )

            try:
                async with asyncio.timeout(settings.research_timeout_seconds):
                    async for ev in agen:
                        if ev.name not in _ALLOWED_EVENTS:
                            log.warning("dropping unexpected agent event %r", ev.name[:40])
                            continue
                        try:
                            node = json.loads(ev.data)
                            if not isinstance(node, dict):
                                raise ValueError("not an object")
                        except ValueError:
                            log.warning("malformed %s event from agent", ev.name)
                            error_text = GENERIC_ERROR
                            break

                        if ev.name == "error":
                            # Never forward agent-supplied text; log it, show a fixed message.
                            log.warning("agent reported error: %s", str(node.get("content", ""))[:200])
                            error_text = GENERIC_ERROR
                            break
                        if ev.name == "done":
                            break

                        problem = _absorb(ev.name, node, res)
                        if problem:
                            error_text = problem
                            break

                        # State is updated before delivery: a client that drops right after
                        # the final event must not cost us the finished answer.
                        yield _sse(ev.name, ev.data)
            except TimeoutError:
                log.warning("research timed out after %ss", settings.research_timeout_seconds)
                error_text = TIMEOUT_ERROR
            except AgentUnavailable as e:
                log.error("research agent unavailable: %s", e)
                error_text = UNAVAILABLE_ERROR
            except AgentProtocolError as e:
                log.error("research agent protocol error: %s", e)
                error_text = GENERIC_ERROR
            except Exception:  # noqa: BLE001 - any other upstream failure: report safely, keep the record
                log.exception("unexpected error while streaming from the research agent")
                error_text = GENERIC_ERROR

            if error_text is None and not res.got_terminal:
                error_text = INTERRUPTED_ERROR
            if error_text is not None:
                yield _err(error_text)

            msg_id = await _persist_with_retry(turn.conv_id, res, error_text)
            if msg_id is not None:
                res.persisted = True
                yield _sse("persisted", json.dumps({"messageId": str(msg_id)}))
            else:
                yield _sse(
                    "persist_error",
                    json.dumps({"status": "failed", "content": "Could not save this answer."}),
                )
        except (asyncio.CancelledError, GeneratorExit):
            cancelled = True
            raise
        except Exception:
            log.exception("research stream failed")
            yield _err(GENERIC_ERROR)
        finally:
            stream_guard.release(key)
            try:
                await asyncio.shield(agen.aclose())  # cancel the upstream agent request promptly
            except BaseException:  # noqa: BLE001 - cleanup must never mask the original exit
                pass
            if cancelled and res.got_terminal and not res.persisted:
                # The client left after the answer was complete: keep it for their history.
                try:
                    await asyncio.shield(
                        run_in_threadpool(_persist_assistant, turn.conv_id, res, error_text)
                    )
                except BaseException:  # noqa: BLE001
                    log.warning("could not persist completed answer after disconnect")

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
