from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

_PLACEHOLDER_MARKERS = ("change-me", "changeme", "secret", "example", "your-")
# Passwords that must never reach a real deployment. Used only to REFUSE them, never as defaults.
_WEAK_DB_PASSWORDS = {"", "lumen", "postgres", "password", "passw0rd", "admin", "root", "changeme", "change-me", "secret", "123456", "12345678", "test", "dev"}


class Settings(BaseSettings):
    # REQUIRED: no default. A default database credential would silently become the production credential.
    database_url: str
    # Explicit opt-in for local development with a weak database password (e.g. the legacy lumen/lumen).
    # Never set this in a shared or production environment.
    allow_insecure_dev_db: bool = False

    # REQUIRED. No default on purpose: the app refuses to start without a strong secret.
    jwt_secret: str
    jwt_expiration_minutes: int = 60 * 24
    jwt_issuer: str = "lumen-backend"
    jwt_audience: str = "lumen-web"

    research_agent_url: str = "http://localhost:8004"
    # Shared secret presented to the research agent as `X-Internal-Token`.
    agent_api_key: str = ""

    cors_allowed_origins: str = "http://localhost:5173"
    port: int = 8080
    enable_hsts: bool = False  # set true only when served over HTTPS

    # Limits
    max_request_bytes: int = 64 * 1024
    max_persisted_blocks_bytes: int = 1024 * 1024
    research_timeout_seconds: int = 300

    # Distributed rate limiting. Empty REDIS_URL = in-process limits only (development/tests).
    redis_url: str = ""
    rate_limit_prefix: str = "lumen:rl"
    redis_timeout_seconds: float = 0.25      # connect and command timeout: Redis trouble must never stall requests
    redis_retry_seconds: float = 5.0         # circuit breaker: after a failure, skip Redis this long
    redis_max_connections: int = 64          # bounded pool per backend process
    # What to do when Redis is unreachable: "local" = enforce per-process limits (bounded, degraded);
    # "closed" = refuse the guarded operation with 503. Never "unlimited".
    rate_limit_fail_mode: str = "local"
    # Reverse proxies whose X-Forwarded-For header may be believed (comma-separated CIDRs). Empty = trust none.
    trusted_proxy_cidrs: str = ""

    # Rate limits
    login_per_ip_per_minute: int = 20
    login_failures_per_email: int = 5
    login_lockout_seconds: int = 15 * 60
    register_per_ip_per_hour: int = 10
    queries_per_user_per_minute: int = 12
    max_concurrent_streams_per_user: int = 2
    max_concurrent_streams_global: int = 200      # all users together: caps paid LLM fan-out
    api_requests_per_user_per_minute: int = 300   # every authenticated call

    # hide_input_in_errors: a rejected DATABASE_URL / JWT_SECRET must never be echoed into logs or tracebacks
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore", hide_input_in_errors=True)

    @field_validator("jwt_secret")
    @classmethod
    def _strong_secret(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters (try: openssl rand -base64 48)")
        low = v.lower()
        if any(m in low for m in _PLACEHOLDER_MARKERS):
            raise ValueError("JWT_SECRET looks like a placeholder; set a real random value")
        return v

    @model_validator(mode="after")
    def _check_db(self) -> "Settings":
        try:
            url = make_url(self.database_url)
        except Exception as e:  # noqa: BLE001
            raise ValueError("DATABASE_URL is not a valid SQLAlchemy URL") from e
        host = (url.host or "").lower()
        is_local = host in ("", "localhost", "127.0.0.1", "::1")
        password = url.password or ""
        if (password.lower() in _WEAK_DB_PASSWORDS or len(password) < 12) and not self.allow_insecure_dev_db:
            raise ValueError(
                "DATABASE_URL uses a missing/weak/default database password. Set a strong one "
                "(openssl rand -hex 24) — or, for local development only, ALLOW_INSECURE_DEV_DB=1."
            )
        if self.allow_insecure_dev_db and not is_local and host not in ("postgres",):
            raise ValueError("ALLOW_INSECURE_DEV_DB is only permitted for local databases (localhost or the compose service)")
        return self

    @field_validator("rate_limit_fail_mode")
    @classmethod
    def _fail_mode(cls, v: str) -> str:
        if v not in ("local", "closed"):
            raise ValueError("RATE_LIMIT_FAIL_MODE must be 'local' or 'closed'")
        return v

    @property
    def cors_origins(self) -> list[str]:
        origins = [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]
        if "*" in origins:
            raise ValueError("CORS_ALLOWED_ORIGINS may not contain '*' when credentials are allowed")
        return origins


settings = Settings()
