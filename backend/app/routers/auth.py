import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import sessions
from ..client_ip import client_ip
from ..config import settings
from ..db import get_db
from ..deps import AuthContext, get_auth_context, get_current_user
from ..models import AuthSession, User
from ..ratelimit import FailureLockout, SlidingWindowLimiter
from ..schemas import AuthResponse, LoginRequest, MeResponse, RegisterRequest
from ..security import decode_token, hash_password, issue_token, verify_dummy, verify_password

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

login_ip_limiter = SlidingWindowLimiter("login-ip", settings.login_per_ip_per_minute, 60)
register_ip_limiter = SlidingWindowLimiter("register-ip", settings.register_per_ip_per_hour, 3600)
login_lockout = FailureLockout("login-email", settings.login_failures_per_email, settings.login_lockout_seconds)
refresh_ip_limiter = SlidingWindowLimiter("refresh-ip", settings.refresh_per_ip_per_minute, 60)
refresh_session_limiter = SlidingWindowLimiter("refresh-session", settings.refresh_per_session_per_minute, 60)
upgrade_ip_limiter = SlidingWindowLimiter("upgrade-ip", settings.login_per_ip_per_minute, 60)


def _too_many(retry_after: int, detail: str = "Too many attempts. Try again later.") -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS, detail, headers={"Retry-After": str(retry_after)}
    )


def _cookies_enabled() -> bool:
    return settings.auth_mode in ("dual", "cookie")


def _finish_login(request: Request, response: Response, db: Session, user: User) -> AuthResponse:
    """Start a cookie session (dual|cookie) and build the body. The legacy 24 h token is returned only to old clients
    (modes bearer|dual, and only when the client did not announce itself with `X-Auth-Mode: cookie`)."""
    if _cookies_enabled():
        session, refresh_token = sessions.create_session(db, user.id, request)
        sessions.set_session_cookies(response, user.id, session, refresh_token)
    new_client = request.headers.get("x-auth-mode", "").strip().lower() == "cookie"
    # bearer mode has no cookies, so the body token is the only credential and is always returned.
    want_token = settings.auth_mode == "bearer" or (settings.auth_mode == "dual" and not new_client)
    return AuthResponse(token=issue_token(user) if want_token else None, id=user.id, email=user.email, name=user.name)


@router.post("/register", response_model=AuthResponse, response_model_exclude_none=True)
def register(req: RegisterRequest, request: Request, response: Response, db: Session = Depends(get_db)) -> AuthResponse:
    decision = register_ip_limiter.hit(client_ip(request))
    if not decision.allowed:
        raise _too_many(decision.retry_after)

    email = req.email.lower().strip()
    if db.query(User).filter(User.email == email).one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    user = User(email=email, password_hash=hash_password(req.password), name=req.name)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Two concurrent registrations for one email: the unique index decides.
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered") from None
    db.refresh(user)
    return _finish_login(request, response, db, user)


@router.post("/login", response_model=AuthResponse, response_model_exclude_none=True)
def login(req: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)) -> AuthResponse:
    decision = login_ip_limiter.hit(client_ip(request))
    if not decision.allowed:
        raise _too_many(decision.retry_after)

    email = req.email.lower().strip()
    if login_lockout.is_locked(email):
        # Same response shape whether or not the account exists.
        raise _too_many(settings.login_lockout_seconds, "Too many failed attempts. Try again later.")

    user = db.query(User).filter(User.email == email).one_or_none()
    if user is None:
        verify_dummy(req.password)  # equalise timing with the real-user path
        login_lockout.record_failure(email)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    if not verify_password(req.password, user.password_hash):
        login_lockout.record_failure(email)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")

    login_lockout.clear(email)
    return _finish_login(request, response, db, user)


@router.get("/me", response_model=MeResponse)
def me(user: User = Depends(get_current_user)) -> MeResponse:
    return MeResponse(id=user.id, email=user.email, name=user.name)


# ── refresh / logout / upgrade ──────────────────────────────────────────────

def _reject(code: int, detail: str) -> JSONResponse:
    """An error response that also clears the session cookies (the browser must not keep dead credentials)."""
    resp = JSONResponse({"detail": detail}, status_code=code, headers={"WWW-Authenticate": "Bearer"} if code == 401 else None)
    sessions.clear_session_cookies(resp)
    return resp


def _stale_token(db: Session, row: AuthSession) -> JSONResponse:
    """A refresh token that was already rotated was presented again."""
    if row.revoked_at is not None:
        return _reject(401, "Invalid or expired session")
    if sessions.within_reuse_grace(row):
        # Another tab/request just rotated it; browsers share cookies, so a retry carries the new token.
        return JSONResponse({"detail": "Refresh in progress"}, status_code=409, headers={"Retry-After": "1"})
    sessions.revoke(db, row.id, "reuse_detected")
    log.warning("refresh token reuse detected: session %s revoked", row.id)  # session id only, never token material
    return _reject(401, "Invalid or expired session")


@router.post("/refresh", response_model=MeResponse)
def refresh(request: Request, response: Response, db: Session = Depends(get_db)):
    if not _cookies_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    names = settings.cookie_names

    decision = refresh_ip_limiter.hit(client_ip(request))
    if not decision.allowed:
        raise _too_many(decision.retry_after)

    token = request.cookies.get(names["refresh"])
    if not token:
        return _reject(401, "Invalid or expired session")
    row, stale = sessions.find_by_refresh_token(db, token)
    if row is None:
        return _reject(401, "Invalid or expired session")
    if stale:
        return _stale_token(db, row)  # checked BEFORE CSRF: any replay of a retired token kills the session
    if not sessions.is_live(row):
        return _reject(401, "Invalid or expired session")

    decision = refresh_session_limiter.hit(str(row.id))
    if not decision.allowed:
        raise _too_many(decision.retry_after)

    if not sessions.csrf_valid(request.headers.get("x-csrf-token"), request.cookies.get(names["csrf"]), row.id):
        log.warning("CSRF validation failed for session %s on refresh", row.id)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF validation failed")

    new_token = sessions.rotate(db, row, sessions.hash_refresh(token))
    if new_token is None:  # lost the race against a concurrent refresh with the same token
        row = db.get(AuthSession, row.id)
        if row is not None and row.prev_refresh_hash == sessions.hash_refresh(token):
            return _stale_token(db, row)
        return _reject(401, "Invalid or expired session")

    user = db.get(User, row.user_id)
    if user is None:
        return _reject(401, "Invalid or expired session")
    sessions.set_session_cookies(response, user.id, row, new_token)
    return MeResponse(id=user.id, email=user.email, name=user.name)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, db: Session = Depends(get_db)) -> Response:
    """Revoke the caller's session (identified by the access cookie, else the refresh cookie) and clear the cookies.
    Idempotent: with no session (or only a legacy Bearer token) it just clears cookies."""
    names = settings.cookie_names
    row: AuthSession | None = None
    if _cookies_enabled():
        access = request.cookies.get(names["access"])
        claims = decode_token(access) if access else None
        if claims and "sid" in claims:
            candidate = db.get(AuthSession, uuid.UUID(claims["sid"]))
            if candidate is not None and str(candidate.user_id) == claims["sub"]:
                row = candidate
        if row is None and request.cookies.get(names["refresh"]):
            row, _stale = sessions.find_by_refresh_token(db, request.cookies[names["refresh"]])
        if row is not None:
            if not sessions.csrf_valid(request.headers.get("x-csrf-token"), request.cookies.get(names["csrf"]), row.id):
                log.warning("CSRF validation failed for session %s on logout", row.id)
                raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF validation failed")
            sessions.revoke(db, row.id, "logout")
    resp = Response(status_code=status.HTTP_204_NO_CONTENT)
    sessions.clear_session_cookies(resp)
    return resp


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
def logout_all(ctx: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)) -> Response:
    sessions.revoke_all_for_user(db, ctx.user.id)
    resp = Response(status_code=status.HTTP_204_NO_CONTENT)
    sessions.clear_session_cookies(resp)
    return resp


def _require_dual() -> None:
    if settings.auth_mode != "dual":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")


@router.post("/session/upgrade", response_model=MeResponse)
def upgrade_session(
    request: Request,
    response: Response,
    _: None = Depends(_require_dual),
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> MeResponse:
    """Dual-mode window only: turn a pre-migration (localStorage Bearer) session into a cookie session."""
    decision = upgrade_ip_limiter.hit(client_ip(request))
    if not decision.allowed:
        raise _too_many(decision.retry_after)
    if ctx.via != "bearer":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Already using a cookie session")
    session, refresh_token = sessions.create_session(db, ctx.user.id, request)
    sessions.set_session_cookies(response, ctx.user.id, session, refresh_token)
    return MeResponse(id=ctx.user.id, email=ctx.user.email, name=ctx.user.name)
