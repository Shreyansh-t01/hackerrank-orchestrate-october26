"""Focused contract tests for local PDF and image text extraction."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from code.evidence_parsers import document_text


class FakeImage:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakePdf:
    page_count = 1

    def __init__(self, page):
        self.page = page

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        return iter([self.page])


class DocumentTextExtractionTests(unittest.TestCase):
    def install_fake_pymupdf(self, page):
        module = ModuleType("pymupdf")
        module.open = lambda _path: FakePdf(page)
        patcher = patch.dict(sys.modules, {"pymupdf": module})
        patcher.start()
        self.addCleanup(patcher.stop)

    def install_fake_pil(self):
        image_module = SimpleNamespace(open=lambda _image: FakeImage())
        pil_module = ModuleType("PIL")
        pil_module.Image = image_module
        patcher = patch.dict(sys.modules, {"PIL": pil_module})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_selectable_pdf_uses_pymupdf_without_ocr(self):
        class Page:
            def get_text(self, _kind):
                return "resource_id: account_17"

            def get_pixmap(self, **_kwargs):
                raise AssertionError("Selectable PDF text should not invoke OCR")

        self.install_fake_pymupdf(Page())
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "selectable.pdf"
            with patch.object(document_text, "_tesseract_text", side_effect=AssertionError("OCR called")):
                pages = document_text.extract_document_pages(source, "evidence_17", "2026-01-02T03:04:05Z")

        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].text, "resource_id: account_17")
        self.assertEqual(pages[0].extraction_method, "pymupdf")
        self.assertEqual(pages[0].page_number, 1)
        self.assertEqual(pages[0].source_filename, "selectable.pdf")
        self.assertEqual(pages[0].evidence_id, "evidence_17")
        self.assertEqual(pages[0].captured_at, "2026-01-02T03:04:05Z")

    def test_scanned_pdf_uses_tesseract_for_page_without_text_layer(self):
        class Pixmap:
            def tobytes(self, _kind):
                return b"rendered-page"

        class Page:
            def get_text(self, _kind):
                return "  "

            def get_pixmap(self, **_kwargs):
                return Pixmap()

        self.install_fake_pymupdf(Page())
        self.install_fake_pil()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "scan.pdf"
            with patch.object(document_text, "_tesseract_text", return_value="scanned account_24"):
                pages = document_text.extract_document_pages(source, "evidence_24", "2026-02-03T04:05:06Z")

        self.assertEqual(pages[0].text, "scanned account_24")
        self.assertEqual(pages[0].extraction_method, "tesseract")
        self.assertEqual(pages[0].page_number, 1)
        self.assertEqual(pages[0].evidence_id, "evidence_24")
        self.assertEqual(pages[0].captured_at, "2026-02-03T04:05:06Z")

    def test_image_uses_local_tesseract_and_keeps_provenance(self):
        self.install_fake_pil()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "screen.png"
            with patch.object(document_text, "_tesseract_text", return_value="ticket 908"):
                pages = document_text.extract_document_pages(source, "evidence_31", "2026-03-04T05:06:07Z")

        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].text, "ticket 908")
        self.assertEqual(pages[0].extraction_method, "tesseract")
        self.assertEqual(pages[0].page_number, 1)
        self.assertEqual(pages[0].source_filename, "screen.png")
        self.assertEqual(pages[0].evidence_id, "evidence_31")
        self.assertEqual(pages[0].captured_at, "2026-03-04T05:06:07Z")

    def test_empty_or_failed_ocr_raises_instead_of_returning_empty_evidence(self):
        class Pixmap:
            def tobytes(self, _kind):
                return b"rendered-page"

        class Page:
            def get_text(self, _kind):
                return ""

            def get_pixmap(self, **_kwargs):
                return Pixmap()

        self.install_fake_pymupdf(Page())
        self.install_fake_pil()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "blank-scan.pdf"
            with patch.object(document_text, "_tesseract_text", return_value=""):
                with self.assertRaisesRegex(document_text.DocumentTextExtractionError, "no readable text"):
                    document_text.extract_document_pages(source, "evidence_45", "2026-04-05T06:07:08Z")
            with patch.object(
                document_text,
                "_tesseract_text",
                side_effect=document_text.DocumentTextExtractionError("engine failed"),
            ):
                with self.assertRaisesRegex(document_text.DocumentTextExtractionError, "engine failed"):
                    document_text.extract_document_pages(source, "evidence_45", "2026-04-05T06:07:08Z")


if __name__ == "__main__":
    unittest.main()
