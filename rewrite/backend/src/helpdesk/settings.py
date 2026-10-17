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
    chat_url: str = ""
    embedding_url: str = ""
    rerank_url: str = ""
    chat_model: str = ""
    embedding_model: str = ""
    rerank_model: str = ""
    chat_request_model: str = ""
    embedding_request_model: str = ""
    rerank_request_model: str = ""
    model_ca_file: Path | None = None
    model_connect_host: str = ""
    model_inference_enabled: bool = True
    model_api_key: SecretStr = SecretStr("")
    chat_api_key: SecretStr = SecretStr("")
    embedding_api_key: SecretStr = SecretStr("")
    rerank_api_key: SecretStr = SecretStr("")
    rerank_format: str = "qwen"
    embedding_dimension: int = Field(default=1024, ge=1, le=8192)
    request_timeout: float = Field(default=110, gt=0, le=110)
    request_calls: int = Field(default=24, ge=1, le=24)
    token_reservation_limit: int = Field(default=48000, gt=0)
    worker_lease_seconds: int = Field(default=90, ge=3, le=300)
    worker_heartbeat_seconds: float = Field(default=20, gt=0)
    worker_job_timeout: float = Field(default=600, ge=1, le=1800)

    @property
    def models_ready(self) -> bool:
        return self.model_inference_enabled and all(
            (
                self.chat_url,
                self.embedding_url,
                self.rerank_url,
                self.chat_model,
                self.embedding_model,
                self.rerank_model,
            )
        )
