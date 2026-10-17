"""Completion cards use downloaded artwork, without another source request."""
import io
import json
from pathlib import Path
import zipfile

from PIL import Image
from jmemby.storage.cbz import contained


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
            picture.convert("RGB").save(output, "JPEG", quality=85)
            return output.getvalue()
    except (OSError, ValueError, KeyError, StopIteration, zipfile.BadZipFile):
        return None
