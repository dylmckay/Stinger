from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache

_env_file = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str
    BACKEND_URL: str = "http://localhost:8000"
    allow_private_targets: bool = Field(False, env="STINGER_ALLOW_PRIVATE")

    model_config = SettingsConfigDict(env_file = str(_env_file), extra = "ignore")

@lru_cache()
def get_settings() -> Settings:
    return Settings()
