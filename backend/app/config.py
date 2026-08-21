"""Application settings."""

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = f"sqlite+aiosqlite:///{Path(__file__).resolve().parent.parent.parent / 'data' / 'snoreader.db'}"
    feed_fetch_interval_minutes: int = 60
    article_retention_days: int = 60
    # 葉ジャンルの未読上限。超えると分割案を作る（一括 triage で確認できる上限）
    genre_unread_limit: int = 50
    host: str = "0.0.0.0"
    port: int = 8000

    llm_base_url: str = "http://localhost:8880/v1"
    llm_model: str = "/Users/sano/models/gemma-4-e4b-it-4bit"
    llm_timeout: int = 120
    # 推論（thinking）の強度。Ollama の OpenAI 互換エンドポイントは "none" で
    # thinking を無効化でき、要約・タグ生成は品質を落とさず 4-5 倍速くなる
    # （qwen3.8:27b-mlx で実測 35 秒 -> 7 秒）。空文字にするとパラメータ自体を
    # 送らないので、reasoning_effort を解釈しないサーバ（mlx-lm.server 等）でも動く。
    llm_reasoning_effort: str = "none"

    model_config = {"env_prefix": "SNOREADER_"}


settings = Settings()
