import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from .config import settings
from .db import engine
from .middleware import BodySizeLimitMiddleware, OriginCheckMiddleware, SecurityHeadersMiddleware
from .ratelimit import RateLimitUnavailable, get_backend
from .routers import auth, conversations, research

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Connect to Redis and load the rate-limit scripts before the first request (never fails startup).
    backend = get_backend()
    if backend.enabled:
        log.info("rate limiting: %s", "Redis (shared across instances)" if backend.warm() else "Redis unreachable at start; using the fallback")
    else:
        log.warning("rate limiting: REDIS_URL is not set; limits are per process only")
    yield


# No interactive docs/OpenAPI in the running service: it would publish the whole API surface.
app = FastAPI(title="Lumen Backend", version="2.1.0", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

# Outermost last: size limit runs before routing, headers wrap every response (incl. errors).
app.add_middleware(OriginCheckMiddleware)  # innermost: runs for every auth type, after CORS has handled preflights
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-CSRF-Token", "X-Auth-Mode"],
    max_age=600,
)
app.add_middleware(SecurityHeadersMiddleware)


@app.exception_handler(OperationalError)
async def _db_unavailable(request: Request, exc: OperationalError) -> JSONResponse:
    log.error("database unavailable on %s %s: %s", request.method, request.url.path, type(exc).__name__)
    return JSONResponse({"detail": "Service temporarily unavailable"}, status_code=503)


@app.exception_handler(SQLAlchemyError)
async def _db_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    log.exception("database error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "Internal server error"}, status_code=500)


@app.exception_handler(RateLimitUnavailable)
async def _limits_unavailable(request: Request, exc: RateLimitUnavailable) -> JSONResponse:
    # RATE_LIMIT_FAIL_MODE=closed and Redis is down: refuse rather than run unlimited.
    log.error("rate-limit store unavailable and fail mode is 'closed': refusing %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "Service temporarily unavailable"}, status_code=503, headers={"Retry-After": "5"})


@app.exception_handler(RequestValidationError)
async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Report which field failed and why, but never echo submitted values (passwords!).
    errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg", "invalid")} for e in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "Internal server error"}, status_code=500)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


@app.get("/health/ready")
def ready() -> JSONResponse:
    """Readiness: the database answers. (Schema is managed by `python -m app.migrate`.)"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        log.error("readiness check failed: %s", type(e).__name__)
        return JSONResponse({"status": "unavailable", "database": "error"}, status_code=503)
    # Redis trouble degrades limiting to per-process (see ratelimit.py) but does not make the service unready.
    return JSONResponse({"status": "ready", "database": "ok", "rate_limit": get_backend().status})


app.include_router(auth.router)
app.include_router(conversations.router)
app.include_router(research.router)
