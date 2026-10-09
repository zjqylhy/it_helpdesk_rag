from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
import zipfile
from pypdf import PdfReader
from docx import Document

PARSER_VERSION = 'paragraph-v1'

@dataclass
class Paragraph:
    text: str
    locator: dict

def parse(path: str) -> list[Paragraph]:
    p = Path(path)
    suffix = p.suffix.lower()
    result = []
    if suffix == '.pdf':
        reader = PdfReader(p)
        if reader.is_encrypted:
            raise ValueError('不支持加密 PDF')
        for page, item in enumerate(reader.pages, 1):
            for ordinal, text in enumerate((item.extract_text() or '').split('\n\n'), 1):
                if text.strip():
                    result.append(Paragraph(text.strip(), {'page': page, 'paragraph': ordinal}))
    elif suffix == '.docx':
        with zipfile.ZipFile(p) as archive:
            if sum(i.file_size for i in archive.infolist()) > 100 * 1024 * 1024:
                raise ValueError('DOCX 解压后过大')
        doc, section = Document(p), ''
        for ordinal, paragraph in enumerate(doc.paragraphs, 1):
            text = paragraph.text.strip()
            if paragraph.style.name.startswith('Heading'):
                section = text
            if text:
                result.append(Paragraph(text, {'paragraph': ordinal, 'section': section}))
    elif suffix in ('.md', '.txt'):
        text = p.read_text(encoding='utf-8-sig')
        section = ''
        for ordinal, paragraph in enumerate(re.split(r'\n\s*\n', text), 1):
            paragraph = paragraph.strip()
            if paragraph.startswith('#'):
                section = paragraph.splitlines()[0].lstrip('# ').strip()
            if paragraph:
                result.append(Paragraph(paragraph, {'paragraph': ordinal, 'section': section}))
    else:
        raise ValueError('文件类型不支持')
    if not result or sum(len(x.text) for x in result) < 10:
        raise ValueError('未提取到足够文本；扫描件需要 OCR，首期不支持')
    return result

def chunk(paragraphs: list[Paragraph], size=700, overlap=120):
    if overlap >= size:
        raise ValueError('切片重叠必须小于长度')
    result = []
    # 独立段落保留原文字符区间，评测锚点可跨重叠切片重新映射。
    for paragraph in paragraphs:
        for start in range(0, len(paragraph.text), size - overlap):
            text = paragraph.text[start:start + size]
            result.append({'text': text, 'locator': {**paragraph.locator, 'start': start, 'end': start + len(text)},
                           'content_hash': sha256(text.encode()).hexdigest()})
            if start + size >= len(paragraph.text):
                break
    return result
