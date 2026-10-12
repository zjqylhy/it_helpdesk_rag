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
    chat_gateway_enabled: bool = False
    retrieval_gateway_enabled: bool = False
    model_gateway_base_url: str = 'https://localhost:9443/v1'
    gateway_api_key: str = Field(default='', validation_alias='IT_HELPDESK_RAG_API_KEY', repr=False)
    gateway_chat_model: str = 'chat'
    gateway_embedding_model: str = 'embedding'
    gateway_rerank_model: str = 'rerank'
    model_gateway_ca_file: str = ''
    # 容器连接宿主机隧道时仍使用逻辑地址的 Host 和 TLS 主机名。
    model_gateway_connect_host: str = ''
    model_timeout: float = 120
    chunk_size: int = Field(default=700, ge=200)
    chunk_overlap: int = Field(default=120, ge=0)
    context_budget: int = Field(default=8000, ge=1000)

    def dsn(self):
        # Go 的 sslmode URL 可直接用于 psycopg；避免把凭据写入日志。
        return self.database_url

    def uses_gateway(self, kind):
        return self.chat_gateway_enabled if kind == 'llm' else self.retrieval_gateway_enabled

    def model_routes(self):
        return {
            kind: {'provider': 'gateway' if self.uses_gateway(kind) else 'direct',
                   'model': alias if self.uses_gateway(kind) else model}
            for kind, alias, model in [('llm', self.gateway_chat_model, self.llm_model),
                                       ('embedding', self.gateway_embedding_model, self.embedding_model),
                                       ('rerank', self.gateway_rerank_model, self.rerank_model)]
        }
