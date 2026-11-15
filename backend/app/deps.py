import logging
import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal, db_gate
from .legacy_usage import record_bearer_use
from .models import AuthSession, User
from .ratelimit import SlidingWindowLimiter
from .security import decode_token
from .sessions import SAFE_METHODS, csrf_valid, is_live

log = logging.getLogger(__name__)

# Every authenticated request counts against the caller (all routes, shared across instances).
api_user_limiter = SlidingWindowLimiter("api-user", settings.api_requests_per_user_per_minute, 60, strict=False)


def _extract_bearer(request: Request) -> str | None:
    """None = no bearer attempt was made. "" = a `Bearer` header with no usable token (still an attempt)."""
    # Header only. A `?token=` query parameter is deliberately NOT accepted: URLs end
    # up in access logs, browser history and Referer headers.
    header = request.headers.get("authorization")
    if header and header[:6].lower() == "bearer":
        return header[6:].strip()
    return None


def _unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"}
    )


@dataclass
class AuthContext:
    user: User
    via: str  # "bearer" | "cookie"
    session: AuthSession | None = None  # set for cookie auth and for bearer tokens that carry a session id


def authenticate(request: Request, db: Session) -> AuthContext:
    """Resolve the caller. Precedence: Authorization header (modes bearer|dual), then the access cookie (dual|cookie).

    * In mode `cookie` the Authorization header is ignored; in mode `bearer` cookies are ignored.
    * Cookie-authenticated unsafe requests must pass CSRF validation.
    """
    mode = settings.auth_mode
    names = settings.cookie_names

    bearer = _extract_bearer(request) if mode in ("bearer", "dual") else None
    if bearer is not None:  # presenting a bearer credential that is bad never falls back to a cookie
        claims = decode_token(bearer) if bearer else None
        if not claims:
            raise _unauthorized()
        session = None
        if "sid" in claims:  # a session-bound token must still have a live session
            session = db.get(AuthSession, uuid.UUID(claims["sid"]))
            if not is_live(session) or str(session.user_id) != claims["sub"]:
                raise _unauthorized()
        user = db.get(User, uuid.UUID(claims["sub"]))
        if not user:
            raise _unauthorized()
        record_bearer_use(session is not None)   # counted only after the token was fully accepted
        return AuthContext(user, "bearer", session)

    cookie = request.cookies.get(names["access"]) if mode in ("dual", "cookie") else None
    if not cookie:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Missing credentials", headers={"WWW-Authenticate": "Bearer"}
        )
    claims = decode_token(cookie)
    if not claims or "sid" not in claims:
        raise _unauthorized()
    sid = uuid.UUID(claims["sid"])
    session = db.get(AuthSession, sid)
    if not is_live(session) or str(session.user_id) != claims["sub"]:
        raise _unauthorized()
    user = db.get(User, uuid.UUID(claims["sub"]))
    if not user:
        raise _unauthorized()
    if request.method not in SAFE_METHODS and not csrf_valid(
        request.headers.get("x-csrf-token"), request.cookies.get(names["csrf"]), sid
    ):
        log.warning("CSRF validation failed for session %s on %s", sid, request.method)  # no token material
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF validation failed")
    return AuthContext(user, "cookie", session)


def _authenticate_once(request: Request) -> AuthContext:
    # Own short-lived session: the connection goes back to the pool before the handler runs (a request must never hold one
    # connection while asking for another). The objects are detached but fully loaded (expire_on_commit=False, no commit here).
    with SessionLocal() as db:
        return authenticate(request, db)


async def get_auth_context(request: Request) -> AuthContext:
    async with db_gate():                  # admission to the database phase; see db.py
        ctx = await run_in_threadpool(_authenticate_once, request)
    decision = await run_in_threadpool(api_user_limiter.hit, str(ctx.user.id))
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many requests. Please slow down.",
            headers={"Retry-After": str(decision.retry_after)},
        )
    return ctx


async def get_current_user(ctx: AuthContext = Depends(get_auth_context)) -> User:
    return ctx.user
