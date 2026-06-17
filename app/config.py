from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache

_env_file = Path(__file__).resolve().parents[1] / ".env"


class EncryptionSettings(BaseSettings):
    SECRET_KEY: str | None = None
    STINGER_ENCRYPTION_KEY: str | None = None

    model_config = SettingsConfigDict(env_file=str(_env_file), extra="ignore")


def encryption_key_material() -> bytes:
    settings = EncryptionSettings()
    key = settings.STINGER_ENCRYPTION_KEY or settings.SECRET_KEY
    if not key:
        raise RuntimeError("set SECRET_KEY or STINGER_ENCRYPTION_KEY to encrypt signing secrets")
    return key.encode()


class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str
    BACKEND_URL: str = "http://localhost:8000"
    allow_private_targets: bool = Field(False, validation_alias="STINGER_ALLOW_PRIVATE")
    STINGER_ENCRYPTION_KEY: str | None = None

    model_config = SettingsConfigDict(env_file=str(_env_file), extra="ignore")

    def encryption_key_material(self) -> bytes:
        return encryption_key_material()

@lru_cache
def get_settings() -> Settings:
    return Settings()
