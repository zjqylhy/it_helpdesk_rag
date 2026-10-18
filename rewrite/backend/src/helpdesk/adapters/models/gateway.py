import json
import math
import re
import time
from collections.abc import AsyncIterator
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from helpdesk.application.model_session import RateLimitedModelError, TransientModelError
from helpdesk.domain.errors import AppError
from helpdesk.domain.models import Row
from helpdesk.domain.rag import Reply, TextDelta, Usage
from helpdesk.settings import Settings


class ProviderResponseError(AppError):
    def __init__(self, known_usage: Usage | None) -> None:
        super().__init__("invalid_provider_response", 502, "模型响应格式无效")
        self.known_usage = known_usage


def provider_error(known_usage: Usage | None = None) -> AppError:
    return ProviderResponseError(known_usage)


def usage(value: object, *, input_only: bool = False) -> Usage:
    if not isinstance(value, dict):
        return Usage(
            output_tokens=0 if input_only else None,
            output_applicable=not input_only,
            output_source="not_applicable" if input_only else None,
        )
    incoming = value.get("prompt_tokens", value.get("input_tokens"))
    outgoing = value.get("completion_tokens", value.get("output_tokens"))

    def valid(x: object) -> int | None:
        return x if type(x) is int and 0 <= x < 2**31 else None

    input_source = "prompt_tokens" if "prompt_tokens" in value else "input_tokens"
    if incoming is None and input_only:
        incoming, input_source = value.get("total_tokens"), "total_tokens_input_only"
    raw_output = valid(outgoing)
    return Usage(
        valid(incoming),
        raw_output if raw_output is not None else 0 if input_only else None,
        output_applicable=not input_only,
        input_source=input_source if valid(incoming) is not None else None,
        output_source=("completion_tokens" if "completion_tokens" in value else "output_tokens")
        if raw_output is not None
        else "not_applicable"
        if input_only
        else None,
        raw_output_tokens=raw_output,
    )


def response_error(response: httpx.Response) -> AppError:
    request_id = response.headers.get("x-request-id", "")
    safe_id = request_id if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id) else None
    if response.status_code == 429:
        raw = response.headers.get("retry-after", "").strip()
        seconds, source = 60.0, "fallback"
        if re.fullmatch(r"[0-9]{1,128}", raw):
            seconds, source = float(raw), "seconds"
        elif len(raw) <= 128:
            try:
                date = parsedate_to_datetime(raw)
                if date.tzinfo is not None:
                    seconds, source = max(0.0, date.timestamp() - time.time()), "http_date"
            except (ValueError, TypeError, OverflowError):
                pass
        return RateLimitedModelError(seconds, source, safe_id)
    if response.status_code in {500, 502, 503, 504}:
        return TransientModelError(response.status_code, safe_id)
    return AppError("provider_error", 502, "模型服务请求失败")


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
            async with self.client.stream(
                "POST", url, json=payload, headers=headers, extensions=extensions
            ) as response:
                if not response.is_success:
                    raise response_error(response)
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
        known = usage(result.get("usage"))
        try:
            choice = result["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise provider_error(known)
            value = json.loads(choice["message"]["content"])
            if not isinstance(value, dict):
                raise provider_error(known)
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise provider_error(known) from error
        return Reply(value, known)

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
        known = usage(result.get("usage"), input_only=True)
        try:
            rows = sorted(result["data"], key=lambda x: x["index"])
            if [r["index"] for r in rows] != list(range(len(texts))) or any(
                type(r["index"]) is not int for r in rows
            ):
                raise provider_error(known)
            vectors = [r["embedding"] for r in rows]
            for vector in vectors:
                if (
                    len(vector) != self.settings.embedding_dimension
                    or not all(type(x) in {int, float} and math.isfinite(x) for x in vector)
                    or not any(vector)
                ):
                    raise provider_error(known)
            return vectors, known
        except (KeyError, TypeError, ValueError) as error:
            raise provider_error(known) from error

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
        known = usage(result.get("usage"), input_only=True)
        try:
            rows = result["output"]["results"] if self.settings.rerank_format == "qwen" else result["results"]
            indices = [r["index"] for r in rows]
            if sorted(indices) != list(range(len(texts))) or any(type(i) is not int for i in indices):
                raise provider_error(known)
            if any(
                type(row.get("relevance_score")) not in {int, float}
                or not math.isfinite(row["relevance_score"])
                for row in rows
            ):
                raise provider_error(known)
            indices = [row["index"] for row in sorted(rows, key=lambda row: -row["relevance_score"])]
        except (KeyError, TypeError, ValueError) as error:
            raise provider_error(known) from error
        return indices, known

    async def generate(self, system: str, payload: Row, limit: int) -> AsyncIterator[TextDelta]:
        if not self.settings.model_inference_enabled:
            raise AppError("inference_disabled", 503, "当前验收环境未启用模型推理")
        body = self.chat_payload(system, payload, limit)
        body.update(stream=True, stream_options={"include_usage": True})
        if not self.settings.chat_url:
            raise AppError("models_not_configured", 503, "模型服务尚未配置")
        try:
            url, headers, extensions = self.target("chat")
            async with self.client.stream(
                "POST", url, json=body, headers=headers, extensions=extensions
            ) as response:
                if not response.is_success:
                    raise response_error(response)
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
