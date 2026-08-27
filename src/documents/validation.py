from __future__ import annotations

import hashlib
import unicodedata

from src.shared import PublicError

from .model import UploadFile, ValidatedFile

MAX_FILE_SIZE = 50 * 1024 * 1024
ACTIVE_VERSION_STATES = frozenset({"UPLOADED", "PARSING", "CHUNKED", "EMBEDDING"})
UPLOAD_VISIBILITIES = frozenset({"PRIVATE", "COLLECTION", "DEPARTMENT", "PUBLIC"})
EDIT_VISIBILITIES = frozenset({"PRIVATE", "PUBLIC"})
FORMATS = {
    "txt": ({"text/plain"}, "TXT"),
    "md": ({"text/markdown", "text/plain"}, "MD"),
    "pdf": ({"application/pdf"}, "PDF"),
    "docx": (
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
        "DOCX",
    ),
}


def fail(code: str) -> None:
    raise PublicError(code)


def _is_iso_control(character: str) -> bool:
    value = ord(character)
    return value <= 0x1F or 0x7F <= value <= 0x9F


def validate_file(upload: UploadFile | None) -> ValidatedFile:
    if upload is None or not upload.data:
        fail("DOCUMENT-FILE-001")
    data = upload.data
    if len(data) > MAX_FILE_SIZE:
        fail("DOCUMENT-FILE-002")
    if upload.filename is None or not upload.filename.strip():
        fail("DOCUMENT-FILE-005")

    filename = unicodedata.normalize("NFC", upload.filename.strip())
    if (
        not filename.strip()
        or any(_is_iso_control(character) for character in filename)
        or ".." in filename
        or "/" in filename
        or "\\" in filename
    ):
        fail("DOCUMENT-FILE-005")

    dot = filename.rfind(".")
    if dot <= 0 or dot == len(filename) - 1:
        fail("DOCUMENT-FILE-003")
    extension = filename[dot + 1 :].lower()
    if extension not in FORMATS:
        fail("DOCUMENT-FILE-003")
    allowed_types, document_type = FORMATS[extension]
    if upload.content_type is None or upload.content_type.lower() not in allowed_types:
        fail("DOCUMENT-FILE-004")

    try:
        digest = hashlib.sha256(data).hexdigest()
    except Exception:
        fail("DOCUMENT-FILE-006")
    return ValidatedFile(
        data,
        filename,
        upload.content_type,
        extension,
        document_type,
        digest,
    )


def normalize_title(title: str | None) -> str:
    normalized = title.strip() if title is not None else ""
    if not normalized or len(normalized) > 500:
        fail("COMMON-002")
    return normalized


def normalize_description(description: str | None) -> str | None:
    if description is None or not description.strip():
        return None
    return description.strip()


def validate_page(page: int, size: int) -> None:
    if page < 0 or not 1 <= size <= 100:
        fail("COMMON-002")

