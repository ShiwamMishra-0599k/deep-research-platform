"""Cookie sessions: refresh-token rotation with reuse detection, CSRF tokens, cookie helpers.

Design (docs/COOKIE_AUTH_MIGRATION_PLAN.md, Option 2):
* The access JWT (15 min) travels in an HttpOnly cookie and carries `sid`; every request checks the session row,
  so revocation (logout, reuse detection, logout-all) is immediate.
* The refresh token (opaque, 256-bit) travels in an HttpOnly cookie scoped to /api/auth. Only its SHA-256 is stored.
  Each use rotates it; presenting a stale one outside the grace window revokes the whole session.
* A CSRF token (readable by the SPA on purpose) is HMAC-bound to the session id; unsafe requests must echo it in a header.
Raw tokens are never stored or logged.
"""
import base64
import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Request, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from .client_ip import client_ip
from .config import settings
from .models import AuthSession
from .security import issue_access_token

log = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
REFRESH_PATH = "/api/auth"


def now() -> datetime:
    return datetime.now(timezone.utc)


# ── tokens ──────────────────────────────────────────────────────────────────

def new_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def hash_refresh(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _csrf_sig(rnd: bytes, sid: uuid.UUID) -> bytes:
    return hmac.new(settings.csrf_secret.encode(), f"{sid}.".encode() + rnd, hashlib.sha256).digest()


def make_csrf_token(sid: uuid.UUID) -> str:
    rnd = secrets.token_bytes(16)
    return f"{_b64(rnd)}.{_b64(_csrf_sig(rnd, sid))}"


def csrf_valid(header: str | None, cookie: str | None, sid: uuid.UUID) -> bool:
    """Double submit + signature bound to this session. Constant-time; callers must not say which part failed."""
    if not header or not cookie or not settings.csrf_secret:
        return False
    equal = hmac.compare_digest(header.encode(), cookie.encode())
    try:
        rnd_b64, sig_b64 = cookie.split(".", 1)
        pad = "=" * (-len(rnd_b64) % 4)
        rnd = base64.urlsafe_b64decode(rnd_b64 + pad)
        expected = _b64(_csrf_sig(rnd, sid))
        sig_ok = hmac.compare_digest(sig_b64.encode(), expected.encode())
    except (ValueError, TypeError):
        sig_ok = False
    return equal and sig_ok


def ip_hash(request: Request) -> bytes:
    return hmac.new(settings.jwt_secret.encode(), b"ip|" + client_ip(request).encode(), hashlib.sha256).digest()


# ── cookies ─────────────────────────────────────────────────────────────────

def set_session_cookies(response: Response, user_id: uuid.UUID, session: AuthSession, refresh_token: str) -> None:
    names = settings.cookie_names
    remaining = int((min(session.idle_expires_at, session.absolute_expires_at) - now()).total_seconds())
    remaining = max(remaining, 1)
    common = {"secure": settings.cookie_secure}  # never a Domain attribute: host-only (required for __Host-)
    response.set_cookie(
        names["access"], issue_access_token(user_id, session.id), max_age=settings.access_token_minutes * 60,
        path="/", httponly=True, samesite=settings.cookie_samesite, **common,
    )
    response.set_cookie(
        names["refresh"], refresh_token, max_age=remaining, path=REFRESH_PATH, httponly=True, samesite="strict", **common,
    )
    response.set_cookie(  # NOT HttpOnly: the SPA copies it into X-CSRF-Token (it is useless without the session)
        names["csrf"], make_csrf_token(session.id), max_age=remaining, path="/", httponly=False,
        samesite=settings.cookie_samesite, **common,
    )


def clear_session_cookies(response: Response) -> None:
    names = settings.cookie_names
    secure = settings.cookie_secure
    response.delete_cookie(names["access"], path="/", httponly=True, samesite=settings.cookie_samesite, secure=secure)
    response.delete_cookie(names["refresh"], path=REFRESH_PATH, httponly=True, samesite="strict", secure=secure)
    response.delete_cookie(names["csrf"], path="/", httponly=False, samesite=settings.cookie_samesite, secure=secure)


# ── session rows ────────────────────────────────────────────────────────────

def create_session(db: Session, user_id: uuid.UUID, request: Request) -> tuple[AuthSession, str]:
    t = now()
    absolute = t + timedelta(days=settings.session_absolute_days)
    token = new_refresh_token()
    row = AuthSession(
        user_id=user_id,
        refresh_hash=hash_refresh(token),
        created_at=t,
        last_used_at=t,
        idle_expires_at=min(t + timedelta(hours=settings.session_idle_hours), absolute),
        absolute_expires_at=absolute,
        user_agent=(request.headers.get("user-agent") or "")[:200] or None,
        ip_hash=ip_hash(request),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, token


def is_live(row: AuthSession | None, t: datetime | None = None) -> bool:
    t = t or now()
    return bool(row and row.revoked_at is None and row.idle_expires_at > t and row.absolute_expires_at > t)


def revoke(db: Session, session_id: uuid.UUID, reason: str) -> None:
    db.execute(
        update(AuthSession).where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now(), revoked_reason=reason)
    )
    db.commit()


def revoke_all_for_user(db: Session, user_id: uuid.UUID, reason: str = "logout_all") -> int:
    res = db.execute(
        update(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now(), revoked_reason=reason)
    )
    db.commit()
    return res.rowcount or 0


def rotate(db: Session, row: AuthSession, presented_hash: bytes) -> str | None:
    """Atomically swap the refresh token. Returns the NEW raw token, or None if another request won the race.

    The compare-and-swap in the WHERE clause is what makes two simultaneous refreshes safe: PostgreSQL
    re-checks the predicate after the first commits, so the second matches zero rows.
    """
    token = new_refresh_token()
    t = now()
    res = db.execute(
        update(AuthSession)
        .where(AuthSession.id == row.id, AuthSession.refresh_hash == presented_hash, AuthSession.revoked_at.is_(None))
        .values(
            prev_refresh_hash=presented_hash,
            refresh_hash=hash_refresh(token),
            rotated_at=t,
            last_used_at=t,
            idle_expires_at=func.least(t + timedelta(hours=settings.session_idle_hours), AuthSession.absolute_expires_at),
        )
    )
    if res.rowcount != 1:
        db.rollback()
        return None
    db.commit()
    db.refresh(row)
    return token


def find_by_refresh_token(db: Session, token: str) -> tuple[AuthSession | None, bool]:
    """(session, is_stale): the session whose CURRENT token matches, else the one whose PREVIOUS token matches."""
    h = hash_refresh(token)
    row = db.scalar(select(AuthSession).where(AuthSession.refresh_hash == h))
    if row is not None:
        return row, False
    prev = db.scalar(select(AuthSession).where(AuthSession.prev_refresh_hash == h))
    return prev, prev is not None


def within_reuse_grace(row: AuthSession) -> bool:
    return bool(row.rotated_at and (now() - row.rotated_at).total_seconds() <= settings.refresh_reuse_grace_seconds)


def purge_expired_sessions(db: Session, retention_days: int = 7) -> int:
    """Delete sessions that ended more than `retention_days` ago. Safe to call from a cron/ops task."""
    cutoff = now() - timedelta(days=retention_days)
    res = db.execute(delete(AuthSession).where(AuthSession.absolute_expires_at < cutoff))
    db.commit()
    return res.rowcount or 0
