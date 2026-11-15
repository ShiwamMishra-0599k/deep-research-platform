"""Streams SSE events from the research-agent service."""
import logging
from collections.abc import AsyncIterator

import httpx

from .config import settings

log = logging.getLogger(__name__)

MAX_EVENT_BYTES = 2 * 1024 * 1024  # a single SSE event larger than this is treated as a protocol error


class AgentUnavailable(Exception):
    """The research agent could not be reached or answered with an error status."""


class AgentProtocolError(Exception):
    """The agent sent something that is not valid SSE."""


class AgentEvent:
    __slots__ = ("name", "data")

    def __init__(self, name: str, data: str) -> None:
        self.name = name
        self.data = data


async def stream_research(
    query: str, history: list[dict[str, str]]
) -> AsyncIterator[AgentEvent]:
    """Yield each complete SSE event from the agent (event:/data: pair).

    Raises AgentUnavailable / AgentProtocolError. Exception messages never contain the
    agent's response body, so they are safe to log but are still not shown to users.
    """
    body = {"query": query, "conversation_history": history}
    timeout = httpx.Timeout(connect=5.0, read=settings.research_timeout_seconds, write=15.0, pool=5.0)
    headers = {"Accept": "text/event-stream", "X-Internal-Token": settings.agent_api_key}

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async with client.stream(
                "POST", f"{settings.research_agent_url}/research", json=body, headers=headers
            ) as resp:
                if resp.status_code // 100 != 2:
                    raise AgentUnavailable(f"agent returned HTTP {resp.status_code}")

                current_event: str | None = None
                data_lines: list[str] = []
                size = 0

                async for raw_line in resp.aiter_lines():
                    size += len(raw_line)
                    if size > MAX_EVENT_BYTES:
                        raise AgentProtocolError("SSE event too large")
                    if raw_line == "":
                        if current_event and data_lines:
                            yield AgentEvent(current_event, "\n".join(data_lines))
                        current_event, data_lines, size = None, [], 0
                    elif raw_line.startswith("event:"):
                        current_event = raw_line[6:].strip()
                    elif raw_line.startswith("data:"):
                        data_lines.append(raw_line[5:].lstrip(" "))
                    # comments (":") and unknown fields are ignored

                if current_event and data_lines:
                    yield AgentEvent(current_event, "\n".join(data_lines))
    except (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.RemoteProtocolError,
        httpx.ReadError,
        httpx.PoolTimeout,
    ) as e:
        raise AgentUnavailable(type(e).__name__) from e
