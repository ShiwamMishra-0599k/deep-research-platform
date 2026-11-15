from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDER_MARKERS = ("change-me", "changeme", "secret", "example", "your-")


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://lumen:lumen@localhost:5433/lumen"

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

    # Rate limits (in-process; see SECURITY_REVIEW.md for the multi-worker caveat)
    login_per_ip_per_minute: int = 20
    login_failures_per_email: int = 5
    login_lockout_seconds: int = 15 * 60
    register_per_ip_per_hour: int = 10
    queries_per_user_per_minute: int = 12
    max_concurrent_streams_per_user: int = 2

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    @field_validator("jwt_secret")
    @classmethod
    def _strong_secret(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters (try: openssl rand -base64 48)")
        low = v.lower()
        if any(m in low for m in _PLACEHOLDER_MARKERS):
            raise ValueError("JWT_SECRET looks like a placeholder; set a real random value")
        return v

    @property
    def cors_origins(self) -> list[str]:
        origins = [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]
        if "*" in origins:
            raise ValueError("CORS_ALLOWED_ORIGINS may not contain '*' when credentials are allowed")
        return origins


settings = Settings()
