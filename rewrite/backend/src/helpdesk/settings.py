from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 仅读取新前缀；不自动载入旧 .env，避免误接业务库或模型网关。
    model_config = SettingsConfigDict(env_prefix="HD_", extra="ignore")
    database_url: SecretStr
    upload_dir: Path
    host: str = "127.0.0.1"
    port: int = Field(default=18080, ge=1, le=65535)
    pool_max: int = Field(default=8, ge=2, le=32)
