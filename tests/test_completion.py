import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import zipfile
from PIL import Image

from jmemby.bot.completion import completion_text, cover_bytes


class CompletionTests(unittest.TestCase):
    def test_caption_contains_only_requested_fields_and_fits_telegram(self):
        task = {"snapshot": json.dumps({"title": "😀" * 500, "authors": ["😀" * 500]})}
        text = completion_text(task)
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 1024)
        self.assertEqual(len(text.splitlines()), 3)
        self.assertTrue(text.startswith("✅ 已完成\n标题："))

    def test_local_first_page_becomes_jpeg_cover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "library").mkdir()
            raw = io.BytesIO()
            Image.new("RGB", (1600, 1200)).save(raw, "PNG")
            with zipfile.ZipFile(root / "library/book.cbz", "w") as archive:
                archive.writestr("000001.png", raw.getvalue())
            store = Mock()
            store.chapters.return_value = [{"path": "book.cbz"}]
            result = cover_bytes(store, {"id": "task"}, root)
            with Image.open(io.BytesIO(result)) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(image.size, (1280, 960))
            store.chapters.return_value = [{"path": "missing.cbz"}]
            self.assertIsNone(cover_bytes(store, {"id": "task"}, root))
