"""HTTP/SSE handler — runs Deep Agent and streams checkpoints + final blocks."""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from research_agent.agent.graph import get_agent
from research_agent.blocks.block_formatter import ToolResult, format_blocks
from research_agent.blocks.checkpoint_formatter import format_tool_end, format_tool_start
from research_agent.blocks.follow_up_generator import generate_follow_ups
from research_agent.config import get_settings

logger = logging.getLogger(__name__)
_s = get_settings()

_THINK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.DOTALL | re.IGNORECASE)
_CLARIFICATION_PREFIX = "[CLARIFICATION]"

# Stable, generic messages: raw exception text never reaches the client.
GENERIC_ERROR = "The research service hit an error. Please try again."
TIMEOUT_ERROR = "The research took too long and was stopped. Try a narrower question."
UNAVAILABLE_ERROR = "The research service is temporarily unavailable. Please try again shortly."


class HistoryEntry(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=_s.MAX_HISTORY_CONTENT_CHARS)


class ResearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=_s.MAX_QUERY_CHARS)
    conversation_history: list[HistoryEntry] = Field(default_factory=list, max_length=_s.MAX_HISTORY_ENTRIES)


def _sse(event: str, data: Any) -> str:
    """Format an SSE message. data is JSON-serialized (so it can never contain a raw newline)."""
    payload = json.dumps(data, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def _build_messages(req: ResearchRequest) -> list:
    messages: list = []
    for entry in req.conversation_history:
        if not entry.content:
            continue
        if entry.role == "user":
            messages.append(HumanMessage(content=entry.content))
        else:
            messages.append(AIMessage(content=entry.content))

    today = datetime.datetime.now(datetime.timezone.utc).strftime("%A, %d %B %Y")
    query = f"{req.query}\n\nContext:\nToday's date: {today}"
    messages.append(HumanMessage(content=query))
    return messages


def _extract_text(output) -> str:
    if output is None or not hasattr(output, "content"):
        return ""
    content = output.content
    if isinstance(content, str):
        return _THINK_RE.sub("", content).strip()
    if isinstance(content, list):
        text_parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return _THINK_RE.sub("", " ".join(p for p in text_parts if p.strip()).strip()).strip()
    return ""


def _as_input_dict(tool_input: Any) -> dict:
    if isinstance(tool_input, dict):
        return tool_input
    if isinstance(tool_input, str):
        return {"query": tool_input}
    return {}


async def stream_research(req: ResearchRequest) -> AsyncIterator[str]:
    """Run the agent, yield SSE events: checkpoint, blocks/clarification, done, error.

    Exactly one terminal event (blocks | clarification | error) is sent, then `done`
    for the non-error paths. Cancellation (client disconnect) closes the agent stream.
    """
    yield _sse("checkpoint", {"status": "in_progress", "content": "Researching your query..."})

    try:
        agent = await get_agent()
    except Exception:
        logger.exception("Failed to initialize agent")
        yield _sse("error", {"status": "failed", "content": UNAVAILABLE_ERROR})
        return

    messages = _build_messages(req)
    final_text = ""
    tool_results: list[ToolResult] = []
    events = agent.astream_events(
        {"messages": messages}, config={"recursion_limit": 50}, version="v2"
    )

    try:
        async with asyncio.timeout(get_settings().REQUEST_TIMEOUT_SECONDS):
            async for event in events:
                etype = event.get("event", "")

                if etype == "on_tool_start":
                    tool_name = event.get("name", "")
                    tool_input = _as_input_dict(event.get("data", {}).get("input", {}))
                    msg_text = format_tool_start(tool_name, tool_input)
                    if msg_text:
                        yield _sse("checkpoint", {"status": "in_progress", "content": msg_text, "tool": tool_name})

                elif etype == "on_tool_end":
                    tool_name = event.get("name", "")
                    raw_output = event.get("data", {}).get("output")
                    tool_output = (
                        str(raw_output.content) if hasattr(raw_output, "content") else str(raw_output or "")
                    )
                    tool_input = _as_input_dict(event.get("data", {}).get("input", {}))
                    tool_results.append(ToolResult(tool_name=tool_name, input=tool_input, output=tool_output))
                    end_msg = format_tool_end(tool_name, tool_output)
                    if end_msg:
                        yield _sse("checkpoint", {"status": "in_progress", "content": end_msg, "tool": tool_name})

                elif etype == "on_chat_model_end":
                    candidate = _extract_text(event.get("data", {}).get("output"))
                    if candidate:
                        final_text = candidate
    except TimeoutError:
        logger.warning("Research timed out after %ss", get_settings().REQUEST_TIMEOUT_SECONDS)
        yield _sse("error", {"status": "failed", "content": TIMEOUT_ERROR})
        return
    except asyncio.CancelledError:
        logger.info("Research cancelled (client disconnected)")
        raise
    except Exception:
        logger.exception("Agent stream failed")
        yield _sse("error", {"status": "failed", "content": GENERIC_ERROR})
        return
    finally:
        # Always release the upstream agent stream, including on client disconnect.
        aclose = getattr(events, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()

    if not final_text:
        final_text = "I couldn't find specific information for your query. Please try rephrasing."

    stripped = final_text.strip()
    if stripped.startswith(_CLARIFICATION_PREFIX):
        question = stripped[len(_CLARIFICATION_PREFIX):].strip()
        yield _sse("clarification", {"status": "completed", "content": question})
        yield _sse("done", {"status": "completed"})
        return

    try:
        research_response = format_blocks(final_text, tool_results, req.query)
        try:
            tool_names = list({r.tool_name for r in tool_results})
            follow_ups = await asyncio.wait_for(
                generate_follow_ups(req.query, final_text, tool_names), timeout=3.0
            )
            research_response.follow_ups = follow_ups
        except Exception:
            logger.debug("Follow-up generation skipped")

        yield _sse("blocks", {"status": "completed", "data": research_response.model_dump()})
    except Exception:
        logger.exception("Block formatting failed")
        yield _sse("error", {"status": "failed", "content": GENERIC_ERROR})
        return

    yield _sse("done", {"status": "completed"})
