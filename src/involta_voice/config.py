from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VOICE_",
        env_file=(".env.local", ".env.voice"),
        extra="ignore",
        populate_by_name=True,
    )
    database_url: SecretStr = Field(validation_alias="DATABASE_URL")
    database_schema: str = Field(default="voice", pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    service_secret: SecretStr
    openai_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="OPENAI_API_KEY")
    data_dir: Path = Path("var/voice")
    transcription_model: Literal["gigaam", "parakeet"] = "gigaam"
    asr_models_dir: Path = Path("var/asr-models")
    asr_cpu_threads: int = Field(default=2, ge=1, le=64)
    asr_beta_enabled: bool = True
    max_audio_bytes: int = 10 * 1024 * 1024
    min_free_bytes: int = 64 * 1024 * 1024
    queue_limit: int = Field(default=1000, ge=1)
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
            raise ValueError("Voice requires PostgreSQL DATABASE_URL")
        return self
