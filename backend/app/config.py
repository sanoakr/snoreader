"""Application settings."""

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = f"sqlite+aiosqlite:///{Path(__file__).resolve().parent.parent.parent / 'data' / 'snoreader.db'}"
    feed_fetch_interval_minutes: int = 60
    host: str = "0.0.0.0"
    port: int = 8000

    llm_base_url: str = "http://localhost:8880/v1"
    llm_model: str = "prism-ml/Ternary-Bonsai-8B-mlx-2bit"
    llm_timeout: int = 120

    summarize_interval_seconds: int = 180
    summarize_batch_size: int = 5

    model_config = {"env_prefix": "SNOREADER_"}


settings = Settings()
