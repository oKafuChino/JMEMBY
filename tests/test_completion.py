import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import zipfile
from PIL import Image

from jmemby.bot.completion import completion_text, cover_bytes, photo_caption, read_cover


class CompletionTests(unittest.TestCase):
    def test_photo_caption_fits_unicode_limit_without_splitting_emoji(self):
        for text in ('short caption', '😀' * 1000, 'a' + '😀' * 1000):
            caption = photo_caption(text)
            self.assertLessEqual(len(caption.encode('utf-16-le')) // 2, 1024)
            self.assertNotIn('\ufffd', caption)
        self.assertEqual(photo_caption('short caption'), 'short caption')

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

    def test_extreme_aspect_ratio_is_padded_for_photo_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'library').mkdir()
            for dimensions in ((1, 1280), (1280, 1)):
                data = io.BytesIO()
                Image.new('RGB', dimensions).save(data, 'PNG')
                with zipfile.ZipFile(root / 'library/book.cbz', 'w') as archive:
                    archive.writestr('000001.png', data.getvalue())
                with Image.open(io.BytesIO(read_cover(root, 'book.cbz'))) as image:
                    self.assertLessEqual(max(image.size) / min(image.size), 20)
                    self.assertEqual(image.format, 'JPEG')

    def test_invalid_or_missing_artwork_returns_no_cover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'library').mkdir()
            for relative in (None, '../outside.cbz', 'missing.cbz'):
                self.assertIsNone(read_cover(root, relative))
            path = root / 'library/broken.cbz'
            path.write_bytes(b'broken archive')
            self.assertIsNone(read_cover(root, 'broken.cbz'))
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('000001.jpg', b'broken image')
            self.assertIsNone(read_cover(root, 'broken.cbz'))
