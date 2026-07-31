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

settings = Settings()
