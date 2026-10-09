from pathlib import Path
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore', populate_by_name=True, env_ignore_empty=True)
    database_url: str = 'postgresql://rag:rag@localhost:5432/rag'
    internal_token: str = ''
    deepseek_base_url: str = 'https://api.deepseek.com'
    deepseek_api_key: str = ''
    llm_model: str = 'deepseek-v4-pro'
    dashscope_base_url: str = ''
    # 阿里凭据兼容用户常用的 QWEN 命名；标准名称优先。
    dashscope_api_key: str = Field(default='', validation_alias=AliasChoices('DASHSCOPE_API_KEY', 'QWEN_API_KEY'))
    embedding_model: str = 'qwen3.7-text-embedding'
    embedding_dimension: int = Field(default=1024, ge=1, le=4096)
    rerank_model: str = 'qwen3.7-text-rerank'
    model_timeout: float = 120
    chunk_size: int = Field(default=700, ge=200)
    chunk_overlap: int = Field(default=120, ge=0)
    context_budget: int = Field(default=8000, ge=1000)

    def dsn(self):
        # Go 的 sslmode URL 可直接用于 psycopg；避免把凭据写入日志。
        return self.database_url
