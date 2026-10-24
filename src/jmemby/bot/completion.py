"""Completion cards use downloaded artwork, without another source request."""
import io
import json
from pathlib import Path
import zipfile

from PIL import Image
from jmemby.domain.errors import JobError
from jmemby.storage.cbz import contained


def photo_caption(text):
    """Fit a photo caption without splitting a UTF-16 surrogate pair."""
    if len(text.encode('utf-16-le')) <= 2048:
        return text
    return text.encode('utf-16-le')[:2046].decode('utf-16-le', errors='ignore') + '…'


def photo_file(content):
    from aiogram.types import BufferedInputFile
    return BufferedInputFile(content, filename='cover.jpg')


def completion_text(task):
    manga = json.loads(task["snapshot"])
    # Leave room for Telegram's 1024 UTF-16-unit photo caption limit.
    title = str(manga["title"])[:300]
    authors = ", ".join(manga.get("authors", []))[:150] or "未知"
    return f"✅ 已完成\n标题：{title}\n作者：{authors}"


def cover_bytes(store, task, storage_root):
    chapters = store.chapters(task["id"])
    if not chapters:
        return None
    return read_cover(storage_root, chapters[0]["path"])


def read_cover(storage_root, relative_path):
    try:
        path = contained(Path(storage_root) / "library", relative_path)
        with zipfile.ZipFile(path) as archive:
            name = next(n for n in archive.namelist() if n.startswith("000001."))
            if archive.getinfo(name).file_size > 30 * 1024 * 1024:
                return None
            raw = archive.read(name)
        with Image.open(io.BytesIO(raw)) as picture:
            if picture.width * picture.height > 50_000_000:
                return None
            picture.thumbnail((1280, 1280))
            output = io.BytesIO()
            with picture.convert('RGB') as rgb:
                # Telegram photos allow an aspect ratio of at most 20:1.
                width, height = rgb.size
                size = (max(width, (height + 19) // 20), max(height, (width + 19) // 20))
                if size != rgb.size:
                    with Image.new('RGB', size, 'white') as padded:
                        padded.paste(rgb, ((size[0] - width) // 2, (size[1] - height) // 2))
                        padded.save(output, 'JPEG', quality=85)
                else:
                    rgb.save(output, 'JPEG', quality=85)
            return output.getvalue()
    except (OSError, ValueError, TypeError, KeyError, StopIteration, zipfile.BadZipFile,
            Image.DecompressionBombError, JobError):
        return None
