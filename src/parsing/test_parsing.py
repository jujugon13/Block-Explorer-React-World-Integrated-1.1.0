from __future__ import annotations

import hashlib
import json
import unittest
from io import BytesIO
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from pypdf import PdfWriter

from src.parsing import (
    ChunkClaim,
    ChunkCreator,
    ChunkingConfig,
    ChunkVersionState,
    MemoryChunkState,
    ParsedSection,
    chunk_sections,
    parse_document,
)
from src.platform import PlatformApp
from src.shared import Principal, PublicError, Request, StorageLocation


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class _Page:
    def __init__(self, text: str | None) -> None:
        self.text = text

    def extract_text(self) -> str | None:
        return self.text


class _Reader:
    is_encrypted = False

    def __init__(self, texts: tuple[str | None, ...]) -> None:
        self.pages = tuple(_Page(text) for text in texts)


def _pdf_writer(*, pages: int = 1, password: str | None = None) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    if password is not None:
        writer.encrypt(password)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _docx(body: str, styles: str = "") -> bytes:
    document = f'<w:document xmlns:w="{_W_NS}"><w:body>{body}</w:body></w:document>'
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)
        if styles:
            archive.writestr(
                "word/styles.xml",
                f'<w:styles xmlns:w="{_W_NS}">{styles}</w:styles>',
            )
    return output.getvalue()


def _error_code(callable_) -> str:
    with unittest.TestCase().assertRaises(PublicError) as raised:
        callable_()
    return raised.exception.code


class ParsingAcceptanceTests(unittest.TestCase):
    def test_AC_DOC_050_non_utf8_txt_is_rejected(self):
        self.assertEqual(
            "DOCUMENT-PARSING-003",
            _error_code(lambda: parse_document(b"valid\xffinvalid", "TXT")),
        )
        app = PlatformApp(lambda request: Principal("worker", user_id=1))
        app.add_route(
            "POST",
            "/parse",
            lambda request: parse_document(b"valid\xffinvalid", "TXT"),
        )
        response = app.handle(Request("POST", "/parse"))
        self.assertEqual(422, response.status)
        self.assertEqual("DOCUMENT-PARSING-003", json.loads(response.body)["code"])

    def test_AC_DOC_051_only_one_leading_bom_is_removed(self):
        sections = parse_document("\ufeff\ufeffbody\ufeff".encode(), "TXT")
        self.assertEqual("\ufeffbody\ufeff", sections[0].text)

    def test_AC_DOC_052_crlf_and_cr_become_lf(self):
        sections = parse_document(b"first\r\nsecond\rthird", "MD")
        self.assertEqual("first\nsecond\nthird", sections[0].text)

    def test_AC_DOC_053_whitespace_only_document_is_rejected(self):
        self.assertEqual(
            "DOCUMENT-PARSING-002",
            _error_code(lambda: parse_document(b" \r\n\t", "TXT")),
        )

    def test_AC_DOC_054_encrypted_pdf_is_rejected(self):
        self.assertEqual(
            "DOCUMENT-PARSING-005",
            _error_code(lambda: parse_document(_pdf_writer(password="secret"), "PDF")),
        )

    def test_AC_DOC_055_scanned_pdf_without_text_is_rejected(self):
        self.assertEqual(
            "DOCUMENT-PARSING-006",
            _error_code(lambda: parse_document(_pdf_writer(), "PDF")),
        )

    def test_AC_DOC_056_pdf_replacement_ratio_above_limit_is_rejected(self):
        reader = _Reader(("\ufffd" * 6 + "a" * 94,))
        with patch("src.parsing.core.PdfReader", return_value=reader):
            self.assertEqual(
                "DOCUMENT-PARSING-008",
                _error_code(lambda: parse_document(b"pdf", "PDF")),
            )

    def test_AC_DOC_057_pdf_replacement_ratio_at_limit_is_allowed(self):
        text = "\ufffd" * 5 + "a" * 95
        with patch("src.parsing.core.PdfReader", return_value=_Reader((text,))):
            self.assertEqual(text, parse_document(b"pdf", "PDF")[0].text)

    def test_AC_DOC_058_pdf_chunks_do_not_cross_pages(self):
        with patch("src.parsing.core.PdfReader", return_value=_Reader(("abcdefgh", "ijklmnop"))):
            sections = parse_document(b"pdf", "PDF")
        chunks = chunk_sections(sections, chunk_size=5, overlap=1)
        self.assertEqual((1, 1, 2, 2), tuple(chunk.page_number for chunk in chunks))
        self.assertTrue(all(set(chunk.text) <= set("abcdefgh") for chunk in chunks[:2]))
        self.assertTrue(all(set(chunk.text) <= set("ijklmnop") for chunk in chunks[2:]))
        self.assertEqual((0, 3), (chunks[0].index, chunks[-1].index))
        self.assertEqual((0, 8, 9, 17), (chunks[0].start, chunks[1].end, chunks[2].start, chunks[3].end))

    def test_AC_DOC_059_docx_heading_starts_new_section(self):
        body = (
            "<w:p><w:r><w:t>Preface</w:t></w:r></w:p>"
            '<w:p><w:pPr><w:pStyle w:val="HeadingFake"/></w:pPr><w:r><w:t>Not a heading</w:t></w:r></w:p>'
            '<w:p><w:pPr><w:pStyle w:val="H1"/></w:pPr><w:r><w:t>Heading</w:t></w:r></w:p>'
            "<w:p><w:r><w:t>Body</w:t></w:r></w:p>"
            '<w:p><w:pPr><w:pStyle w:val="T1"/></w:pPr><w:r><w:t>Title</w:t></w:r></w:p>'
        )
        styles = (
            '<w:style w:styleId="H1"><w:name w:val="Heading 1"/></w:style>'
            '<w:style w:styleId="T1"><w:name w:val="TiTlE"/></w:style>'
        )
        sections = parse_document(_docx(body, styles), "DOCX")
        self.assertEqual(
            ("Preface\nNot a heading", "Heading\nBody", "Title"),
            tuple(s.text for s in sections),
        )
        self.assertEqual((None, "Heading", "Title"), tuple(s.section_title for s in sections))

    def test_AC_DOC_060_docx_table_uses_tabs_and_newlines(self):
        body = (
            "<w:p><w:r><w:t>Before</w:t></w:r></w:p>"
            "<w:sdt><w:sdtContent><w:p><w:r><w:t>Wrapped</w:t></w:r></w:p>"
            "</w:sdtContent></w:sdt>"
            "<w:tbl>"
            "<w:tr><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>B</w:t></w:r></w:p></w:tc></w:tr>"
            "<w:tr><w:tc><w:p><w:r><w:t>C</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>D</w:t></w:r></w:p></w:tc></w:tr>"
            "</w:tbl>"
            "<w:customXml><w:p><w:r><w:t>Custom</w:t></w:r></w:p></w:customXml>"
            "<w:p><w:r><w:t>After</w:t></w:r>"
            "<w:drawing><w:txbxContent><w:p><w:r><w:t>Ignored image text</w:t></w:r></w:p>"
            "</w:txbxContent></w:drawing></w:p>"
        )
        self.assertEqual(
            "Before\nWrapped\nA\tB\nC\tD\nCustom\nAfter",
            parse_document(_docx(body), "DOCX")[0].text,
        )

    def test_AC_DOC_061_chunk_indices_ranges_and_hashes_are_deterministic(self):
        sections = (ParsedSection("one two three", section_title="title"),)
        first = chunk_sections(sections, chunk_size=7, overlap=2)
        second = chunk_sections(sections, chunk_size=7, overlap=2)
        self.assertEqual(first, second)
        self.assertEqual((0, 1, 2), tuple(chunk.index for chunk in first))
        self.assertEqual(((0, 7), (5, 12), (10, 13)), tuple((c.start, c.end) for c in first))
        self.assertEqual(
            tuple(hashlib.sha256(chunk.text.encode()).hexdigest() for chunk in first),
            tuple(chunk.text_sha256 for chunk in first),
        )
        self.assertEqual((2, 2, 1), tuple(chunk.token_estimate for chunk in first))
        self.assertEqual(("title", "title", "title"), tuple(chunk.section_title for chunk in first))

    def test_AC_DOC_062_supplementary_character_is_not_split(self):
        chunks = chunk_sections((ParsedSection("A😀B"),), chunk_size=2, overlap=0)
        self.assertEqual(("A😀", "B"), tuple(chunk.text for chunk in chunks))
        self.assertEqual("A😀B", "".join(chunk.text for chunk in chunks))

    def test_AC_DOC_063_exact_multiple_has_no_overlap_only_chunk(self):
        chunks = chunk_sections((ParsedSection("abcdefgh"),), chunk_size=4, overlap=2)
        self.assertEqual(("abcd", "cdef", "efgh"), tuple(chunk.text for chunk in chunks))

    def test_AC_DOC_064_overlap_at_least_chunk_size_fails_validation(self):
        for overlap in (1000, 1001, -1):
            with self.subTest(overlap=overlap), self.assertRaises(ValueError):
                ChunkingConfig(chunk_size=1000, overlap=overlap)


class _PipelineStorage:
    provider = "local"
    namespace = "vectorshelf"

    def __init__(self, data: bytes, on_get=None) -> None:
        self.data = data
        self.get_count = 0
        self.on_get = on_get

    def ensure_location(self, location: StorageLocation) -> None:
        if location.provider != self.provider or location.namespace != self.namespace:
            raise AssertionError("location mismatch")

    def put(self, key: str, data: bytes, expected_size: int) -> StorageLocation:
        raise AssertionError("chunk creation must not write source objects")

    def get(self, location: StorageLocation) -> bytes:
        self.ensure_location(location)
        self.get_count += 1
        if self.on_get:
            self.on_get()
        return self.data

    def delete(self, location: StorageLocation) -> None:
        raise AssertionError("chunk creation must not delete source objects")


def _pipeline_state(data: bytes = b"abcdefgh"):
    location = StorageLocation("local", "vectorshelf", "documents/file.txt", len(data))
    row = ChunkVersionState(10, 20, 30, 40, "token", "TXT", location)
    return row, MemoryChunkState(row), ChunkClaim(10, 20, 30, 40, "token")


class ChunkPipelineAcceptanceTests(unittest.TestCase):
    def test_AC_DOC_061_chunk_commit_is_atomic_and_replay_skips_storage(self):
        row, state, claim = _pipeline_state()
        storage = _PipelineStorage(b"abcdefgh")
        creator = ChunkCreator(storage, state, chunk_size=4, overlap=2)

        created = creator.create(claim)
        self.assertEqual(201, created.status)
        self.assertEqual("CHUNKED", row.status)
        self.assertEqual(("PARSE_STARTED", "CHUNKED"), tuple(row.events))
        self.assertEqual(created.chunks, row.chunks)
        self.assertEqual(1, storage.get_count)

        replayed = creator.create(claim)
        self.assertEqual(200, replayed.status)
        self.assertEqual(created.chunks, replayed.chunks)
        self.assertEqual(1, storage.get_count)
        self.assertEqual(("PARSE_STARTED", "CHUNKED"), tuple(row.events))

    def test_AC_DOC_053_zero_chunk_draft_never_commits(self):
        row, state, claim = _pipeline_state(b"content")
        storage = _PipelineStorage(b"content")
        creator = ChunkCreator(
            storage,
            state,
            parser=lambda data, kind: (),
            chunk_size=4,
            overlap=1,
        )
        with self.assertRaises(PublicError) as raised:
            creator.create(claim)
        self.assertEqual("DOCUMENT-PARSING-002", raised.exception.code)
        self.assertEqual("PARSING", row.status)
        self.assertEqual((), row.chunks)
        self.assertEqual(["PARSE_STARTED"], row.events)

    def test_AC_DOC_061_ownership_is_rechecked_after_external_read(self):
        row, state, claim = _pipeline_state()
        storage = _PipelineStorage(
            b"abcdefgh", on_get=lambda: setattr(row, "claim_token", "lost")
        )
        creator = ChunkCreator(storage, state, chunk_size=4, overlap=2)
        with self.assertRaises(PublicError) as raised:
            creator.create(claim)
        self.assertEqual("EMBEDDING-JOB-003", raised.exception.code)
        self.assertEqual("PARSING", row.status)
        self.assertEqual((), row.chunks)
        self.assertEqual(["PARSE_STARTED"], row.events)

    def test_AC_DOC_064_chunk_creator_rejects_invalid_startup_configuration(self):
        _, state, _ = _pipeline_state()
        storage = _PipelineStorage(b"abcdefgh")
        for overlap in (1000, 1001):
            with self.subTest(overlap=overlap), self.assertRaises(ValueError):
                ChunkCreator(storage, state, chunk_size=1000, overlap=overlap)
        self.assertEqual(0, storage.get_count)


if __name__ == "__main__":
    unittest.main()
