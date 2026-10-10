import asyncio
from contextlib import asynccontextmanager
import hmac
import json
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from .config import Settings
from .providers import Models, ProviderError
from .store import Store
from .worker import worker
from .pipeline import Pipeline
from .access import AuthorizedStore, AccessDenied

settings = Settings()
store = Store(settings)

@asynccontextmanager
async def lifespan(app):
    if len(settings.internal_token) < 24:
        raise RuntimeError('INTERNAL_TOKEN 至少 24 字符')
    models = Models(settings)
    task = asyncio.create_task(worker(store, models))
    yield
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await models.close()

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
indices = {}
online_limit = asyncio.Semaphore(3)
embedding_limit = asyncio.Semaphore(2)

class Chat(BaseModel):
    kb_id: str | None = None
    kb_ids: list[str] = Field(default_factory=list, max_length=32)
    question: str = Field(min_length=1, max_length=12000)
    history: list[dict[str,str]] = Field(default_factory=list, max_length=8)
    request_id: str
    user_id: str = Field(min_length=1, max_length=128)
    authorization_revision: int = Field(ge=0)

@app.get('/health')
async def health():
    await store.rows('SELECT 1')
    return {'status': 'ok'}

@app.post('/internal/chat')
async def chat(body: Chat, x_internal_token: str = Header(default='')):
    if not hmac.compare_digest(x_internal_token, settings.internal_token):
        raise HTTPException(401)
    if body.kb_id and body.kb_ids:
        raise HTTPException(400, detail='知识范围重复')
    if any(not value.strip() or len(value) > 128 for value in body.kb_ids):
        raise HTTPException(400, detail='知识范围无效')
    authorized = AuthorizedStore(settings, body.user_id, body.authorization_revision)
    try:
        await authorized.bind()
    except AccessDenied:
        raise HTTPException(403, detail='资料不存在或不可访问')
    async def stream():
        # 每请求独立用量，避免并发会话的 Token 统计混合。
        models = Models(settings)
        models.online = online_limit
        models.embeddings_limit = embedding_limit
        pipeline = Pipeline(authorized, models)
        pipeline.indices = indices
        try:
            async for event, data in pipeline.events(body.kb_id, body.question, body.history, kb_ids=body.kb_ids):
                yield f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'
        except (ProviderError, ValueError) as exc:
            yield f'event: error\ndata: {json.dumps({"error": str(exc)}, ensure_ascii=False)}\n\n'
        except Exception:
            yield 'event: error\ndata: {"error":"检索服务处理失败，回答未验证"}\n\n'
        finally:
            await models.close()
    return StreamingResponse(stream(), media_type='text/event-stream')
