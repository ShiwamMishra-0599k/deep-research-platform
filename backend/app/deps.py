import uuid

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import User
from .ratelimit import SlidingWindowLimiter
from .security import decode_token

# Every authenticated request counts against the caller (all routes, shared across instances).
api_user_limiter = SlidingWindowLimiter("api-user", settings.api_requests_per_user_per_minute, 60, strict=False)


def _extract_token(request: Request) -> str | None:
    # Header only. A `?token=` query parameter is deliberately NOT accepted: URLs end
    # up in access logs, browser history and Referer headers.
    header = request.headers.get("authorization")
    if header and header[:7].lower() == "bearer ":
        return header[7:].strip() or None
    return None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = _extract_token(request)
    if not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Missing credentials", headers={"WWW-Authenticate": "Bearer"}
        )
    claims = decode_token(token)
    if not claims:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"}
        )
    user = db.get(User, uuid.UUID(claims["sub"]))
    if not user:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"}
        )
    decision = api_user_limiter.hit(str(user.id))
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many requests. Please slow down.",
            headers={"Retry-After": str(decision.retry_after)},
        )
    return user
