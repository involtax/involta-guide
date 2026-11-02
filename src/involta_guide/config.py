from base64 import b64decode
from pathlib import Path

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env.local", extra="ignore", case_sensitive=False)
    database_url: SecretStr
    test_database_url: SecretStr = SecretStr("")
    database_schema: str = "public"
    session_secret: SecretStr
    admin_password: SecretStr = SecretStr("")
    admin_password_hash: SecretStr = SecretStr("")
    alex_secret: SecretStr = SecretStr("")
    alex_secret_base64: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-4.1"
    transcription_model: str = "gpt-4o-mini-transcribe"
    site_url: str = "http://localhost:3000/guide"
    base_path: str = "/guide"
    release_sha: str = "development"
    # TEMPORARY: explicitly requested anonymous, one-location-at-a-time initial fill.
    temporary_public_fill_enabled: bool = True
    cookie_secure: bool = False
    data_dir: Path = Path("var")
    audio_retention_days: int = 90
    max_audio_bytes: int = 10 * 1024 * 1024
    pending_review_limit: int = 1000
    daily_ai_calls: int = 1000
    ai_daily_budget_usd: float = 20
    ai_prices_json: str = "{}"
    trusted_proxy_ips: str = "127.0.0.1"
    worker_heartbeat_file: Path = Path("/tmp/involta-worker-heartbeat")
    lease_seconds: int = 240
    max_job_attempts: int = 3
    root_dir: Path = Path(__file__).resolve().parents[2]

    @field_validator("release_sha")
    @classmethod
    def release_valid(cls, value: str) -> str:
        import re

        if value != "development" and not re.fullmatch(r"[0-9a-f]{40}", value):
            raise ValueError("RELEASE_SHA должен быть полным commit SHA")
        return value

    @field_validator("base_path")
    @classmethod
    def path_valid(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"/[a-zA-Z0-9/_-]+", value) or value.endswith("/"):
            raise ValueError("BASE_PATH: путь без завершающего слэша")
        return value

    @field_validator("database_schema")
    @classmethod
    def schema_valid(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value):
            raise ValueError("Некорректная PostgreSQL-схема")
        return value

    @model_validator(mode="after")
    def validate_secrets(self) -> "Settings":
        from urllib.parse import urlsplit

        encoded_alex = self.alex_secret_base64.get_secret_value()
        if encoded_alex:
            try:
                self.alex_secret = SecretStr(b64decode(encoded_alex, validate=True).decode("utf-8"))
            except ValueError as exc:
                raise ValueError("Invalid ALEX_SECRET_BASE64 encoding") from exc
        if len(self.session_secret.get_secret_value()) < 32:
            raise ValueError("SESSION_SECRET должен содержать минимум 32 символа")
        if not self.database_url.get_secret_value().startswith(
            ("postgresql://", "postgresql+psycopg://")
        ):
            raise ValueError("Нужен PostgreSQL DATABASE_URL")
        path = urlsplit(self.site_url).path.rstrip("/")
        if path not in ("", self.base_path):
            raise ValueError("SITE_URL и BASE_PATH не совпадают")
        return self

    def path(self, path: str = "/") -> str:
        return self.base_path + path

    def url(self, path: str = "/") -> str:
        from urllib.parse import urlsplit

        site = urlsplit(self.site_url)
        return f"{site.scheme}://{site.netloc}{self.path(path)}"
