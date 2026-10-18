import hashlib
import io
import zipfile
from pathlib import PurePosixPath

from helpdesk.domain.errors import AppError
from helpdesk.domain.models import Row
from helpdesk.domain.rag import ParsedChunk

MAX_TEXT = 2_000_000


def parse(data: bytes, filename: str) -> list[ParsedChunk]:
    if len(data) > 20 * 1024 * 1024:
        raise AppError("file_too_large", 413, "文件超出解析上限")
    suffix = PurePosixPath(filename).suffix.lower()
    paragraphs: list[tuple[str, Row]] = []
    if suffix in {".txt", ".md"}:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise AppError("invalid_encoding", 400, "文本必须为 UTF-8") from error
        paragraphs = [
            (line.strip(), {"paragraph": i + 1}) for i, line in enumerate(text.splitlines()) if line.strip()
        ]
    elif suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted or len(reader.pages) > 1000:
            raise AppError("unsupported_pdf", 400, "PDF 加密或页数超过限制")
        total = 0
        for i, page in enumerate(reader.pages):
            text = (page.extract_text() or "").strip()
            total += len(text)
            if total > MAX_TEXT:
                raise AppError("text_too_large", 400, "解析文本超出上限")
            if text:
                paragraphs.append((text, {"page": i + 1, "paragraph": len(paragraphs) + 1}))
    elif suffix == ".docx":
        from docx import Document

        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(x.file_size for x in archive.infolist()) > 64 * 1024 * 1024:
                raise AppError("docx_too_large", 400, "DOCX 解压内容超出上限")
        document = Document(io.BytesIO(data))
        paragraphs = [
            (p.text.strip(), {"paragraph": i + 1})
            for i, p in enumerate(document.paragraphs)
            if p.text.strip()
        ]
    else:
        raise AppError("unsupported_file", 400, "不支持的解析格式")
    full = "\n\n".join(text for text, _ in paragraphs)
    if not full.strip():
        raise AppError("empty_document", 400, "没有可解析的正文；扫描件需先提供文本版本")
    if len(full) > MAX_TEXT:
        raise AppError("text_too_large", 400, "解析文本超出上限")
    chunks: list[ParsedChunk] = []
    # 字符位置属于原段落；不跨段拼接，避免改变黄金出处的坐标含义。
    for paragraph, location in paragraphs:
        for start in range(0, len(paragraph), 580):
            end = min(start + 700, len(paragraph))
            text = paragraph[start:end]
            locator = {**location, "start": start, "end": end}
            chunks.append(ParsedChunk(len(chunks), text, locator, hashlib.sha256(text.encode()).hexdigest()))
            if end == len(paragraph):
                break
    return chunks
