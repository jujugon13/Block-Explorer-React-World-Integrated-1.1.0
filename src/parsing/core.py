"""VectorShelf parsing and chunk calculation from §§7.2 and 9.3."""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from io import BytesIO
from zipfile import BadZipFile, ZipFile

from pypdf import PdfReader

from src.shared import PublicError


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_PDF_REPLACEMENT_LIMIT = 0.05
_IGNORED_DOCX_CONTENT = {f"{_W}drawing", f"{_W}pict", f"{_W}object"}
_DOCX_BODY_WRAPPERS = {
    f"{_W}sdt",
    f"{_W}sdtContent",
    f"{_W}customXml",
    f"{_W}ins",
    f"{_W}del",
    f"{_W}moveFrom",
    f"{_W}moveTo",
}


@dataclass(frozen=True, slots=True)
class ParsedSection:
    text: str
    page_number: int | None = None
    section_title: str | None = None


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    start: int
    end: int
    text: str
    text_sha256: str
    token_estimate: int
    page_number: int | None
    section_title: str | None


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    chunk_size: int = 1000
    overlap: int = 200

    def __post_init__(self) -> None:
        validate_chunk_config(self.chunk_size, self.overlap)


def validate_chunk_config(chunk_size: int = 1000, overlap: int = 200) -> None:
    """Fail startup validation when a chunk window cannot advance."""
    if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("chunk_size must be positive and 0 <= overlap < chunk_size")


def parse_document(data: bytes, document_type: str) -> tuple[ParsedSection, ...]:
    """Parse one supported document into page or heading-bounded sections."""
    kind = document_type.upper() if isinstance(document_type, str) else ""
    if kind in {"TXT", "MD"}:
        return (ParsedSection(_parse_utf8(data)),)
    if kind == "PDF":
        return _parse_pdf(data)
    if kind == "DOCX":
        return _parse_docx(data)
    raise PublicError("DOCUMENT-PARSING-001")


def chunk_sections(
    sections: tuple[ParsedSection, ...] | list[ParsedSection],
    chunk_size: int = 1000,
    overlap: int = 200,
) -> tuple[Chunk, ...]:
    """Split sections without crossing their boundaries, using global offsets."""
    config = ChunkingConfig(chunk_size, overlap)
    ordered = tuple(sections)
    stride = config.chunk_size - config.overlap
    chunks: list[Chunk] = []
    base = 0

    for section_index, section in enumerate(ordered):
        local_start = 0
        while local_start < len(section.text):
            local_end = min(local_start + config.chunk_size, len(section.text))
            text = section.text[local_start:local_end]
            chunks.append(
                Chunk(
                    index=len(chunks),
                    start=base + local_start,
                    end=base + local_end,
                    text=text,
                    text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    token_estimate=len(re.findall(r"\S+", text)),
                    page_number=section.page_number,
                    section_title=section.section_title,
                )
            )
            if local_end == len(section.text):
                break
            local_start += stride

        base += len(section.text)
        if section_index < len(ordered) - 1:
            base += 1

    return tuple(chunks)


def _parse_utf8(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except (AttributeError, UnicodeDecodeError):
        raise PublicError("DOCUMENT-PARSING-003") from None
    if text.startswith("\ufeff"):
        text = text[1:]
    text = _normalize_newlines(text)
    if not text.strip():
        raise PublicError("DOCUMENT-PARSING-002")
    return text


def _parse_pdf(data: bytes) -> tuple[ParsedSection, ...]:
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise PublicError("DOCUMENT-PARSING-005")
        sections = tuple(
            ParsedSection(text, page_number=number)
            for number, page in enumerate(reader.pages, start=1)
            if (text := _normalize_newlines(page.extract_text() or "").strip())
        )
    except PublicError:
        raise
    except Exception:
        raise PublicError("DOCUMENT-PARSING-007") from None

    if not sections:
        raise PublicError("DOCUMENT-PARSING-006")
    text = "".join(section.text for section in sections)
    if text.count("\ufffd") / len(text) > _PDF_REPLACEMENT_LIMIT:
        raise PublicError("DOCUMENT-PARSING-008")
    return sections


def _parse_docx(data: bytes) -> tuple[ParsedSection, ...]:
    try:
        with ZipFile(BytesIO(data)) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
            styles, default_style = _docx_styles(archive)
    except (AttributeError, BadZipFile, KeyError, OSError, RuntimeError, ValueError, ET.ParseError):
        raise PublicError("DOCUMENT-PARSING-007") from None

    body = root.find(f"{_W}body")
    if body is None:
        raise PublicError("DOCUMENT-PARSING-007")

    sections: list[ParsedSection] = []
    blocks: list[str] = []
    title: str | None = None

    def flush() -> None:
        if blocks and (text := "\n".join(blocks)).strip():
            sections.append(ParsedSection(text, section_title=title))

    for element in _body_elements(body):
        if element.tag == f"{_W}p":
            text = _normalize_newlines(_paragraph_text(element))
            style_id = element.find(f"./{_W}pPr/{_W}pStyle")
            style = style_id.get(f"{_W}val", "") if style_id is not None else ""
            style_name = styles.get(style, default_style if not style else "").casefold()
            if style_name.startswith("heading") or style_name == "title":
                flush()
                blocks = [text]
                title = text
            else:
                blocks.append(text)
        elif element.tag == f"{_W}tbl":
            blocks.append(_table_text(element))

    flush()
    if not sections:
        raise PublicError("DOCUMENT-PARSING-002")
    return tuple(sections)


def _body_elements(parent: ET.Element):
    for element in parent:
        if element.tag in {f"{_W}p", f"{_W}tbl"}:
            yield element
        elif element.tag in _DOCX_BODY_WRAPPERS:
            yield from _body_elements(element)


def _docx_styles(archive: ZipFile) -> tuple[dict[str, str], str]:
    try:
        root = ET.fromstring(archive.read("word/styles.xml"))
    except KeyError:
        return {}, ""
    styles: dict[str, str] = {}
    default_style = ""
    for style in root.findall(f"{_W}style"):
        name = style.find(f"{_W}name")
        if name is not None:
            style_name = name.get(f"{_W}val", "")
            styles[style.get(f"{_W}styleId", "")] = style_name
            if (
                style.get(f"{_W}type") == "paragraph"
                and style.get(f"{_W}default") in {"1", "true"}
            ):
                default_style = style_name
    return styles, default_style


def _paragraph_text(paragraph: ET.Element) -> str:
    parts: list[str] = []

    def visit(parent: ET.Element) -> None:
        for node in parent:
            if node.tag in _IGNORED_DOCX_CONTENT:
                continue
            if node.tag == f"{_W}t":
                parts.append(node.text or "")
            elif node.tag == f"{_W}tab":
                parts.append("\t")
            elif node.tag in {f"{_W}br", f"{_W}cr"}:
                parts.append("\n")
            visit(node)

    visit(paragraph)
    return "".join(parts)


def _table_text(table: ET.Element) -> str:
    rows: list[str] = []
    for row in table.findall(f"./{_W}tr"):
        cells: list[str] = []
        for cell in row.findall(f"./{_W}tc"):
            cells.append("\n".join(_paragraph_text(p) for p in cell.findall(f"./{_W}p")))
        rows.append("\t".join(cells))
    return "\n".join(rows)


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")
