from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from .dburl import resolve_database_url

_PLACEHOLDER_MARKERS = ("change-me", "changeme", "secret", "example", "your-")
# Passwords that must never reach a real deployment. Used only to REFUSE them, never as defaults.
_WEAK_DB_PASSWORDS = {"", "lumen", "postgres", "password", "passw0rd", "admin", "root", "changeme", "change-me", "secret", "123456", "12345678", "test", "dev"}


class Settings(BaseSettings):
    # "production" turns unsafe settings into startup errors (see _check_production). Anything else = development.
    environment: str = "development"

    # REQUIRED: no default. A default database credential would silently become the production credential.
    # Either DATABASE_URL, or DB_HOST + DB_PASSWORD (+ DB_PORT/DB_NAME/DB_USER): the component form matches
    # AWS Secrets Manager / RDS secrets (see app/dburl.py and docs/AWS_SECRETS_PLAN.md).
    database_url: str = ""
    # Explicit opt-in for local development with a weak database password (e.g. the legacy lumen/lumen).
    # Never set this in a shared or production environment.
    allow_insecure_dev_db: bool = False

    # REQUIRED. No default on purpose: the app refuses to start without a strong secret.
    jwt_secret: str
    jwt_expiration_minutes: int = 60 * 24
    jwt_issuer: str = "lumen-backend"
    jwt_audience: str = "lumen-web"

    # ── Sessions (see docs/COOKIE_AUTH_MIGRATION_PLAN.md) ────────────────────────────────────────────
    # bearer = legacy header JWT only | dual = header JWT AND cookie sessions (migration window) | cookie = cookies only
    auth_mode: str = "dual"
    access_token_minutes: int = 15            # life of the access JWT carried in the access cookie
    session_idle_hours: int = 24              # sliding inactivity timeout, extended at every refresh
    session_absolute_days: int = 7            # hard cap on a session, whatever the activity
    refresh_reuse_grace_seconds: int = 10     # a just-rotated token seen again inside this window = concurrent tab, not theft
    # Housekeeping for the auth_sessions table. Rows are kept this long after the session ended (reuse-detection evidence).
    session_purge_retention_days: int = 7
    # In-process purge loop period. 0 disables it (use `python -m app.cleanup` from cron / a scheduled task instead).
    session_purge_interval_seconds: int = 3600
    cookie_secure: bool = False               # true in production: Secure flag + __Host-/__Secure- name prefixes
    cookie_samesite: str = "lax"              # access + CSRF cookies. The refresh cookie is always Strict.
    # Signs CSRF tokens. Distinct from jwt_secret. REQUIRED whenever auth_mode != "bearer".
    csrf_secret: str = ""
    refresh_per_ip_per_minute: int = 30
    refresh_per_session_per_minute: int = 10

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

    # How long a request may wait for a free database slot before it gets 503 + Retry-After (see db.py).
    db_gate_wait_seconds: float = 10.0

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

    @model_validator(mode="before")
    @classmethod
    def _database_from_components(cls, data):
        if isinstance(data, dict) and not data.get("database_url") and not data.get("DATABASE_URL"):
            resolved = resolve_database_url()
            if resolved:
                data = {**data, "database_url": resolved}
        return data

    @field_validator("environment")
    @classmethod
    def _environment(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("development", "production"):
            raise ValueError("ENVIRONMENT must be 'development' or 'production'")
        return v

    @model_validator(mode="after")
    def _check_production(self) -> "Settings":
        """In production the safe choices are mandatory, not defaults someone can forget to set."""
        if self.environment != "production":
            return self
        problems = []
        if self.rate_limit_fail_mode != "closed":
            problems.append("RATE_LIMIT_FAIL_MODE must be 'closed' (login/register/research must refuse, not degrade, when Redis is down)")
        if not self.redis_url:
            problems.append("REDIS_URL is required (shared rate limits)")
        if self.allow_insecure_dev_db:
            problems.append("ALLOW_INSECURE_DEV_DB must not be set")
        if not self.enable_hsts:
            problems.append("ENABLE_HSTS must be true (serve over HTTPS)")
        bad = [o for o in self.cors_origins if not o.lower().startswith("https://")]
        if bad:
            problems.append("CORS_ALLOWED_ORIGINS must all be https:// origins")
        if not self.agent_api_key or len(self.agent_api_key) < 24:
            problems.append("AGENT_API_KEY must be set (>= 24 chars)")
        if self.auth_mode != "bearer" and not self.cookie_secure:
            problems.append("COOKIE_SECURE must be true when cookie sessions are enabled (AUTH_MODE != bearer)")
        if problems:
            raise ValueError("unsafe production configuration: " + "; ".join(problems))
        return self

    @model_validator(mode="after")
    def _check_db(self) -> "Settings":
        if not self.database_url:
            raise ValueError("database_url is required: set DATABASE_URL, or DB_HOST and DB_PASSWORD")
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

    @field_validator("auth_mode")
    @classmethod
    def _auth_mode(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("bearer", "dual", "cookie"):
            raise ValueError("AUTH_MODE must be 'bearer', 'dual' or 'cookie'")
        return v

    @field_validator("cookie_samesite")
    @classmethod
    def _samesite(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("lax", "strict"):
            raise ValueError("COOKIE_SAMESITE must be 'lax' or 'strict' ('none' is not supported)")
        return v

    @model_validator(mode="after")
    def _check_sessions(self) -> "Settings":
        if self.access_token_minutes < 1 or self.session_idle_hours < 1 or self.session_absolute_days < 1:
            raise ValueError("session lifetimes must be positive")
        if self.session_purge_retention_days < 1 or self.session_purge_interval_seconds < 0:
            raise ValueError("SESSION_PURGE_RETENTION_DAYS must be >= 1 and SESSION_PURGE_INTERVAL_SECONDS >= 0")
        if self.auth_mode == "bearer":
            return self
        s = self.csrf_secret
        if len(s) < 32:
            raise ValueError("CSRF_SECRET must be at least 32 characters when cookie sessions are enabled (AUTH_MODE != bearer)")
        if any(m in s.lower() for m in _PLACEHOLDER_MARKERS):
            raise ValueError("CSRF_SECRET looks like a placeholder; set a real random value")
        if s == self.jwt_secret:
            raise ValueError("CSRF_SECRET must differ from JWT_SECRET")
        return self

    @property
    def cookie_names(self) -> dict[str, str]:
        if self.cookie_secure:  # browsers enforce these prefixes: Secure + (for __Host-) Path=/ and no Domain
            return {"access": "__Host-lumen_at", "refresh": "__Secure-lumen_rt", "csrf": "__Host-lumen_csrf"}
        return {"access": "lumen_at", "refresh": "lumen_rt", "csrf": "lumen_csrf"}

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
