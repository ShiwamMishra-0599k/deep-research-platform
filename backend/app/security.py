import uuid
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from .config import settings
from .models import User

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
_ALG = "HS256"

# bcrypt only reads the first 72 bytes; longer passwords would be silently truncated.
MAX_PASSWORD_BYTES = 72

# Verified against when the account does not exist, so login timing does not reveal
# whether an email is registered.
_DUMMY_HASH = _pwd_ctx.hash("lumen-dummy-password-for-timing")


def hash_password(plain: str) -> str:
    return _pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _pwd_ctx.verify(plain, hashed)
    except ValueError:
        return False


def verify_dummy(plain: str) -> None:
    """Spend the same time as a real verification (used for unknown emails)."""
    try:
        _pwd_ctx.verify(plain, _DUMMY_HASH)
    except ValueError:
        pass


def issue_access_token(user_id: uuid.UUID, sid: uuid.UUID) -> str:
    """Short-lived JWT carried in the HttpOnly access cookie. `sid` ties it to a revocable server-side session."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "sid": str(sid),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_minutes)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALG)


def issue_token(user: User) -> str:
    """LEGACY 24 h bearer token (no session id). Only returned to old clients during the dual-mode window."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_expiration_minutes)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALG)


def decode_token(token: str) -> dict | None:
    """Return validated claims, or None for any invalid/expired/foreign token."""
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[_ALG],  # pinned: rejects `alg: none` and algorithm-confusion
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require_exp": True, "require_sub": True, "require_iat": True},
        )
    except JWTError:
        return None
    try:
        uuid.UUID(str(claims.get("sub")))
        if "sid" in claims:
            uuid.UUID(str(claims["sid"]))
    except ValueError:
        return None
    return claims
