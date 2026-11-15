"""FastAPI server — Redis lifespan + POST /research SSE endpoint.

This service is INTERNAL: only the backend calls it. Every endpoint except /health
requires the shared secret (header X-Internal-Token == env AGENT_API_KEY). With no
AGENT_API_KEY configured the service fails closed (503). There is no CORS
middleware because browsers must never talk to this service directly.
"""
from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager

import coloredlogs
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import ValidationError

from research_agent.config import get_settings
from research_agent.http_handler import ResearchRequest, stream_research
from research_agent.infrastructure.redis_client import (
    connect_redis,
    disconnect_redis,
    redis_ping,
)

settings = get_settings()
coloredlogs.install(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Lumen Research Agent")
    if not get_settings().AGENT_API_KEY:
        logger.error("AGENT_API_KEY is not set: /research will answer 503 until it is configured")
    # Never raises: if Redis is down we run uncached and reconnect lazily.
    await connect_redis()
    logger.info("Research Agent ready on port %d", settings.PORT)
    yield
    await disconnect_redis()
    logger.info("Research Agent stopped")


def require_internal_token(x_internal_token: str | None = Header(default=None)) -> None:
    key = get_settings().AGENT_API_KEY
    if not key:
        raise HTTPException(status_code=503, detail="Service not configured")
    if not x_internal_token or not hmac.compare_digest(x_internal_token.encode(), key.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized")


async def _read_limited_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="Request body too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail="Request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Lumen Research Agent",
        version="1.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    Instrumentator().instrument(app)

    @app.get("/health", tags=["health"])
    async def health():
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"], dependencies=[Depends(require_internal_token)])
    async def health_ready():
        s = get_settings()
        redis_ok = await redis_ping()
        llm_ok = bool(s.OPENROUTER_API_KEY or s.OPENAI_API_KEY)
        checks = {
            "redis": "ok" if redis_ok else "unavailable (running uncached)",
            "llm": "configured" if llm_ok else "missing",
            "search": "configured" if (s.EXA_API_KEY or s.TAVILY_API_KEY) else "duckduckgo-fallback-only",
        }
        if not llm_ok:
            return JSONResponse({"status": "not_ready", "checks": checks}, status_code=503)
        degraded = (not redis_ok) or checks["search"] != "configured"
        return {"status": "degraded" if degraded else "ready", "checks": checks}

    @app.get("/metrics", tags=["health"], dependencies=[Depends(require_internal_token)])
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/research", tags=["research"], dependencies=[Depends(require_internal_token)])
    async def research(request: Request):
        """Stream research progress as SSE.

        Events: `checkpoint` (progress), `blocks` (final blocks), `clarification`,
        `error`, `done`.
        """
        body = await _read_limited_body(request, get_settings().MAX_BODY_BYTES)
        try:
            req = ResearchRequest.model_validate_json(body)
        except ValidationError as e:
            # Report location + message only; never echo the submitted input.
            detail = [{"loc": list(err["loc"]), "msg": err["msg"]} for err in e.errors()]
            raise HTTPException(status_code=422, detail=detail) from None
        return StreamingResponse(
            stream_research(req),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app
