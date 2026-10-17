import asyncio
import base64
import json
import os
import subprocess
import sys

from helpdesk.domain.errors import AppError
from helpdesk.domain.rag import ParsedChunk


def isolated_parse(data: bytes, filename: str) -> list[ParsedChunk]:
    env = {
        k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG"}
    }
    payload = json.dumps({"filename": filename, "data": base64.b64encode(data).decode()}).encode()
    try:
        response = subprocess.run(
            [sys.executable, "-I", "-m", "helpdesk.entrypoints.parse"],
            input=payload,
            capture_output=True,
            timeout=30,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            check=True,
        )
        result = json.loads(response.stdout)
        if "error" in result or not result.get("chunks"):
            raise ValueError("parse_failed")
        return [ParsedChunk(**row) for row in result["chunks"]]
    except (ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        raise AppError("parse_failed", 400, "文件解析失败、超时或没有可提取正文") from error


async def parse_file(data: bytes, filename: str) -> list[ParsedChunk]:
    # 线程取消不能杀掉子进程；等其 30 秒硬超时完成，避免失租约后累积解析进程。
    task = asyncio.create_task(asyncio.to_thread(isolated_parse, data, filename))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise
