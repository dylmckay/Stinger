from pathlib import Path
from pydantic import ConfigDict
from pydantic_settings import BaseSettings
from functools import lru_cache

_env_file = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str
    SMTP_HOST: str
    SMTP_PORT: int
    SMTP_USER: str
    SMTP_PASSWORD: str
    BACKEND_URL: str = "http://localhost:8000"

    model_config = ConfigDict(env_file = str(_env_file), extra = "ignore")

@lru_cache()
def get_settings() -> Settings:
    return Settings()
