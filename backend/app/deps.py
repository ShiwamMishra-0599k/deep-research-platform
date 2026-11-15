import uuid

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .db import get_db
from .models import User
from .security import decode_token


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
    return user
