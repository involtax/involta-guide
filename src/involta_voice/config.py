from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VOICE_", env_file=".env.voice", extra="ignore")
    database_url: SecretStr
    service_secret: SecretStr
    openai_api_key: SecretStr = SecretStr("")
    data_dir: Path = Path("var/voice")
    transcription_model: str = "gpt-4o-mini-transcribe"
    analysis_model: str = "gpt-4.1"
    max_audio_bytes: int = 10 * 1024 * 1024
    min_free_bytes: int = 64 * 1024 * 1024
    queue_limit: int = Field(default=1000, ge=1)
    daily_calls: int = Field(default=100, ge=1)
    daily_budget_usd: float = Field(default=20, gt=0)
    per_call_reserve_usd: float = Field(default=2, gt=0)
    concurrency: int = Field(default=2, ge=1, le=10)
    lease_seconds: int = Field(default=180, ge=120)
    heartbeat_file: Path = Path("/tmp/involta-voice-worker")
    fake_provider: bool = False

    @model_validator(mode="after")
    def secrets(self) -> "Settings":
        if len(self.service_secret.get_secret_value()) < 32:
            raise ValueError("VOICE_SERVICE_SECRET requires at least 32 characters")
        if not self.database_url.get_secret_value().startswith(
            ("postgresql://", "postgresql+psycopg://")
        ):
            raise ValueError("Voice requires a separate PostgreSQL database")
        return self
