from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    # App
    APP_NAME: str = "Dunemachines File Server"
    VERSION: str = "1.0.0"
    DEBUG: bool = False
    PORT: int = 8007

    # Database
    DATABASE_URL: str

    # omnius_db (dunemachines_backend) — read-only, live org_id resolution.
    # Optional: resolve_identity() fails open to the token's org_id claim
    # (or per-user derivation) when unset or unreachable.
    OMNIUS_DB_URL: Optional[str] = None

    # Redis
    REDIS_URL: str = "redis://localhost:6379/2"

    # Storage (MinIO/S3)
    STORAGE_ENDPOINT: str = "http://localhost:9000"
    STORAGE_ACCESS_KEY: str
    STORAGE_SECRET_KEY: str
    STORAGE_BUCKET: str = "dunemachines-files"
    STORAGE_REGION: str = "us-east-1"
    STORAGE_PUBLIC_ENDPOINT: str = "http://localhost:9000"

    # Auth
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days

    # Duniverse integration
    DUNIVERSE_JWT_SECRET: str

    # Qdrant
    QDRANT_URL: str = "http://76.13.17.48:7333"
    QDRANT_API_KEY: Optional[str] = None

    # Public web app base URL — used to build shareable presentation-link
    # URLs (https://{PUBLIC_APP_URL}/p/{token}). No existing setting covered
    # this (STORAGE_PUBLIC_ENDPOINT is the CDN/object-storage domain, not
    # the web app).
    PUBLIC_APP_URL: str = "https://app.dunemachines.com"

    # File limits
    MAX_FILE_SIZE_MB: int = 500
    ALLOWED_MIME_TYPES: list = []

    # Mistral
    MISTRAL_API_KEY: str
    RATE_LIMIT_SEARCH: int = 60  # per minute
    RATE_LIMIT_UPLOAD: int = 20  # per minute

    # GitHub App
    GITHUB_APP_ID: str = "3765696"
    GITHUB_APP_PRIVATE_KEY: str = ""
    GITHUB_WEBHOOK_SECRET: str
    GITHUB_CLIENT_ID: str
    GITHUB_CLIENT_SECRET: str

    class Config:
        env_file = ".env"

    @property
    def github_private_key(self) -> str:
        try:
            with open("/etc/dunemachines/github_private_key.pem", "r") as f:
                return f.read()
        except FileNotFoundError:
            return self.GITHUB_APP_PRIVATE_KEY

def qdrant_requires_api_key(qdrant_url: str) -> bool:
    """True if `qdrant_url` isn't one of the addresses treated as safe to
    reach without an API key (localhost/127.0.0.1/a unix socket) — a
    pure, directly-testable predicate for the startup guard below."""
    is_local = (
        "localhost" in qdrant_url
        or "127.0.0.1" in qdrant_url
        or qdrant_url.startswith("unix://")
    )
    return not is_local


settings = Settings()

# Finding #8 (security audit, 2026-08), revised to hard-fail: confirmed via
# live netstat that Qdrant is genuinely reachable on a non-localhost
# address with no auth — a warning wasn't enough once that was verified,
# not just theoretical.
if qdrant_requires_api_key(settings.QDRANT_URL) and not settings.QDRANT_API_KEY:
    raise RuntimeError(
        "QDRANT_URL is non-localhost but QDRANT_API_KEY is unset — Qdrant is exposed without auth"
    )
