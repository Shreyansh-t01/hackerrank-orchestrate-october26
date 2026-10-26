import os
import unittest
from unittest.mock import patch

from code.llm.config import (
    DEFAULT_DOCUMENT_MAX_RETRIES,
    DEFAULT_DOCUMENT_TEXT_TIMEOUT_SECONDS,
    get_document_max_retries,
    get_document_text_timeout_seconds,
)


class DocumentTextConfigTests(unittest.TestCase):
    def test_document_timeout_default_and_override(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_document_text_timeout_seconds(), DEFAULT_DOCUMENT_TEXT_TIMEOUT_SECONDS)
        with patch.dict(os.environ, {"GEMINI_DOCUMENT_TEXT_TIMEOUT_SECONDS": "90"}, clear=True):
            self.assertEqual(get_document_text_timeout_seconds(), 90)

    def test_document_retries_default_and_clamp(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_document_max_retries(), DEFAULT_DOCUMENT_MAX_RETRIES)
        with patch.dict(os.environ, {"GEMINI_DOCUMENT_MAX_RETRIES": "99"}, clear=True):
            self.assertEqual(get_document_max_retries(), 5)
