import json
import math
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

import httpx

from helpdesk.application.model_session import TransientModelError
from helpdesk.domain.errors import AppError
from helpdesk.domain.models import Row
from helpdesk.domain.rag import Reply, TextDelta, Usage
from helpdesk.settings import Settings


def provider_error() -> AppError:
    return AppError("invalid_provider_response", 502, "模型响应格式无效")


def usage(value: object) -> Usage:
    if not isinstance(value, dict):
        return Usage()
    incoming = value.get("prompt_tokens", value.get("input_tokens"))
    outgoing = value.get("completion_tokens", value.get("output_tokens"))

    def valid(x: object) -> int | None:
        return x if type(x) is int and 0 <= x < 2**31 else None

    return Usage(valid(incoming), valid(outgoing))


async def bounded_lines(response: httpx.Response) -> AsyncIterator[str]:
    buffer = b""
    # 不指定 chunk_size，避免为凑足字节块而推迟短 token 的首字输出。
    async for block in response.aiter_bytes():
        buffer += block
        if len(buffer) > 512 * 1024:
            raise provider_error()
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            try:
                yield line.removesuffix(b"\r").decode("utf-8")
            except UnicodeDecodeError as error:
                raise provider_error() from error
    if buffer:
        try:
            yield buffer.decode("utf-8")
        except UnicodeDecodeError as error:
            raise provider_error() from error


class HttpModels:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings, self.client = settings, client
        for url in [settings.chat_url, settings.embedding_url, settings.rerank_url]:
            if not url:
                continue
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("模型端点必须是无内嵌凭据的 HTTP(S) 地址")
            if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("远程模型端点必须使用 HTTPS")

    def headers(self, kind: str) -> dict[str, str]:
        specific = getattr(self.settings, kind + "_api_key").get_secret_value()
        key = specific or self.settings.model_api_key.get_secret_value()
        return {"Authorization": "Bearer " + key} if key else {}

    def target(self, kind: str) -> tuple[httpx.URL, dict[str, str], dict]:
        url = httpx.URL(getattr(self.settings, kind + "_url"))
        headers = self.headers(kind)
        extensions: dict = {}
        if self.settings.model_connect_host:
            headers["Host"] = url.netloc.decode("ascii")
            extensions["sni_hostname"] = url.host
            url = url.copy_with(host=self.settings.model_connect_host)
        return url, headers, extensions

    async def post(self, kind: str, payload: Row) -> Row:
        if not self.settings.model_inference_enabled:
            raise AppError("inference_disabled", 503, "当前验收环境未启用模型推理")
        url = getattr(self.settings, kind + "_url")
        if not url:
            raise AppError("models_not_configured", 503, "模型服务尚未配置")
        try:
            url, headers, extensions = self.target(kind)
            async with self.client.stream("POST", url, json=payload, headers=headers, extensions=extensions) as response:
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise TransientModelError()
                if not response.is_success:
                    raise AppError("provider_error", 502, "模型服务请求失败")
                raw = bytearray()
                async for block in response.aiter_bytes(chunk_size=65536):
                    raw.extend(block)
                    if len(raw) > 8 * 1024 * 1024:
                        raise provider_error()
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise TransientModelError() from error
        try:
            value = json.loads(raw)
        except ValueError as error:
            raise provider_error() from error
        if not isinstance(value, dict):
            raise provider_error()
        return value

    def chat_payload(self, system: str, payload: Row, limit: int) -> Row:
        return {
            "model": self.settings.chat_request_model or self.settings.chat_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "temperature": 0,
            "stream": False,
            "max_tokens": limit,
        }

    async def structured(self, stage: str, system: str, payload: Row, limit: int) -> Reply:
        body = self.chat_payload(system + "\n仅返回 JSON 对象。", payload, limit)
        body["response_format"] = {"type": "json_object"}
        result = await self.post("chat", body)
        try:
            choice = result["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise provider_error()
            value = json.loads(choice["message"]["content"])
            if not isinstance(value, dict):
                raise provider_error()
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise provider_error() from error
        return Reply(value, usage(result.get("usage")))

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], Usage]:
        result = await self.post(
            "embedding",
            {
                "model": self.settings.embedding_request_model or self.settings.embedding_model,
                "input": texts,
                "dimensions": self.settings.embedding_dimension,
                "encoding_format": "float",
            },
        )
        try:
            rows = sorted(result["data"], key=lambda x: x["index"])
            if [r["index"] for r in rows] != list(range(len(texts))) or any(
                type(r["index"]) is not int for r in rows
            ):
                raise provider_error()
            vectors = [r["embedding"] for r in rows]
            for vector in vectors:
                if (
                    len(vector) != self.settings.embedding_dimension
                    or not all(type(x) in {int, float} and math.isfinite(x) for x in vector)
                    or not any(vector)
                ):
                    raise provider_error()
            return vectors, usage(result.get("usage"))
        except (KeyError, TypeError, ValueError) as error:
            raise provider_error() from error

    async def rank(self, query: str, texts: list[str]) -> tuple[list[int], Usage]:
        body: Row
        if self.settings.rerank_format == "qwen":
            body = {
                "model": self.settings.rerank_request_model or self.settings.rerank_model,
                "input": {"query": query, "documents": texts},
                "parameters": {"top_n": len(texts)},
            }
        elif self.settings.rerank_format == "generic":
            body = {
                "model": self.settings.rerank_request_model or self.settings.rerank_model,
                "query": query,
                "documents": texts,
                "top_n": len(texts),
                "return_documents": False,
            }
        else:
            raise ValueError("未知重排协议")
        result = await self.post("rerank", body)
        try:
            rows = result["output"]["results"] if self.settings.rerank_format == "qwen" else result["results"]
            indices = [r["index"] for r in rows]
            if sorted(indices) != list(range(len(texts))) or any(type(i) is not int for i in indices):
                raise provider_error()
            if any(
                type(row.get("relevance_score")) not in {int, float}
                or not math.isfinite(row["relevance_score"])
                for row in rows
            ):
                raise provider_error()
            indices = [row["index"] for row in sorted(rows, key=lambda row: -row["relevance_score"])]
        except (KeyError, TypeError, ValueError) as error:
            raise provider_error() from error
        return indices, usage(result.get("usage"))

    async def generate(self, system: str, payload: Row, limit: int) -> AsyncIterator[TextDelta]:
        if not self.settings.model_inference_enabled:
            raise AppError("inference_disabled", 503, "当前验收环境未启用模型推理")
        body = self.chat_payload(system, payload, limit)
        body.update(stream=True, stream_options={"include_usage": True})
        if not self.settings.chat_url:
            raise AppError("models_not_configured", 503, "模型服务尚未配置")
        try:
            url, headers, extensions = self.target("chat")
            async with self.client.stream("POST", url, json=body, headers=headers, extensions=extensions) as response:
                if not response.is_success:
                    raise AppError("provider_error", 502, "模型流请求失败")
                data: list[str] = []
                size = 0
                async for line in bounded_lines(response):
                    size += len(line.encode())
                    if size > 512 * 1024:
                        raise provider_error()
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip(" "))
                    elif not line and data:
                        raw = "\n".join(data)
                        data = []
                        size = 0
                        if raw == "[DONE]":
                            return
                        try:
                            event = json.loads(raw)
                            choices = event.get("choices", [])
                            if choices:
                                choice = choices[0]
                                delta = choice.get("delta", {}).get("content") or ""
                                if not isinstance(delta, str):
                                    raise provider_error()
                                yield TextDelta(delta, choice.get("finish_reason"), usage(event.get("usage")))
                            elif event.get("usage"):
                                yield TextDelta(usage=usage(event["usage"]))
                        except (ValueError, TypeError, KeyError, AttributeError) as error:
                            raise provider_error() from error
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise AppError("provider_stream_error", 502, "模型流连接中断") from error
        raise AppError("incomplete_generation", 502, "模型流缺少结束标记")
