from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8004
    LOG_LEVEL: str = "INFO"

    # Backend -> agent shared secret. Fail closed: if empty, /research returns 503.
    AGENT_API_KEY: str = ""

    # Request limits
    MAX_BODY_BYTES: int = 512 * 1024
    MAX_QUERY_CHARS: int = 4000
    MAX_HISTORY_ENTRIES: int = 40
    MAX_HISTORY_CONTENT_CHARS: int = 20000
    REQUEST_TIMEOUT_SECONDS: float = 240.0

    # Redis
    REDIS_URL: str = "redis://localhost:6379"
    REDIS_SOCKET_TIMEOUT: float = 1.5

    # LLM — OpenRouter primary
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL: str = "anthropic/claude-sonnet-4-6"
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    LLM_TEMPERATURE: float = 0.3

    # LLM — OpenAI fallback
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o"

    # Exa (primary web search)
    EXA_API_KEY: str = ""
    EXA_MAX_RESULTS: int = 5

    # Tavily (fallback web search)
    TAVILY_API_KEY: str = ""
    TAVILY_MAX_RESULTS: int = 5
    TAVILY_SEARCH_DEPTH: str = "advanced"

    # Jina Reader (optional — free tier works without key)
    JINA_API_KEY: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
