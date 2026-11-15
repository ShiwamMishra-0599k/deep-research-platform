import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..client_ip import client_ip
from ..config import settings
from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..ratelimit import FailureLockout, SlidingWindowLimiter
from ..schemas import AuthResponse, LoginRequest, MeResponse, RegisterRequest
from ..security import hash_password, issue_token, verify_dummy, verify_password

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

login_ip_limiter = SlidingWindowLimiter("login-ip", settings.login_per_ip_per_minute, 60)
register_ip_limiter = SlidingWindowLimiter("register-ip", settings.register_per_ip_per_hour, 3600)
login_lockout = FailureLockout("login-email", settings.login_failures_per_email, settings.login_lockout_seconds)


def _too_many(retry_after: int, detail: str = "Too many attempts. Try again later.") -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS, detail, headers={"Retry-After": str(retry_after)}
    )


@router.post("/register", response_model=AuthResponse)
def register(req: RegisterRequest, request: Request, db: Session = Depends(get_db)) -> AuthResponse:
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
    return AuthResponse(token=issue_token(user), id=user.id, email=user.email, name=user.name)


@router.post("/login", response_model=AuthResponse)
def login(req: LoginRequest, request: Request, db: Session = Depends(get_db)) -> AuthResponse:
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
    return AuthResponse(token=issue_token(user), id=user.id, email=user.email, name=user.name)


@router.get("/me", response_model=MeResponse)
def me(user: User = Depends(get_current_user)) -> MeResponse:
    return MeResponse(id=user.id, email=user.email, name=user.name)
