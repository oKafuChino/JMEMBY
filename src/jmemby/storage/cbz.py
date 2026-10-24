from __future__ import annotations

from html import escape
import hashlib
import io
import json
import os
from pathlib import Path
import re
import zipfile
import tempfile

from jmemby.domain.errors import JobError

from jmemby.domain.models import Chapter, Manga, normalize_tags

_INVALID = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")


def safe_component(value: str, fallback: str = "untitled", limit: int = 120) -> str:
    cleaned = _INVALID.sub("_", value).strip().rstrip(".")
    # Linux NAME_MAX is measured in bytes, not Unicode code points.
    cleaned = re.sub(r"\s+", " ", cleaned).encode("utf-8")[:limit].decode("utf-8", errors="ignore").strip().rstrip(".")
    if cleaned.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}:
        cleaned = "_" + cleaned
    return cleaned if cleaned not in ("", ".", "..") else fallback


def id_component(value: str) -> str:
    return safe_component(value, limit=24) + "-" + hashlib.sha256(value.encode()).hexdigest()[:12]


def library_directory(root: Path, manga: Manga) -> Path:
    return root / f"{safe_component(manga.title, limit=70)} [{id_component(manga.source)}-{id_component(manga.source_id)}]"


def chapter_filename(chapter: Chapter, manga: Manga | None = None) -> str:
    suffix = f" [JM{safe_component(manga.source_id, limit=24)}]" if manga and manga.source == "jmcomic" else ""
    return f"{chapter.ordinal:04d} - {safe_component(chapter.title, limit=70)} [{id_component(chapter.source_id)}]{suffix}.cbz"


def contained(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()) or target == root.resolve():
        raise JobError("INVALID_PATH", "文件路径超出漫画库")
    return target


def validate_image_file(path: Path, max_bytes=30 * 1024 * 1024) -> None:
    """Cheap file checks; pixel validation must run in a decoder or controller."""
    if path.is_symlink():
        raise JobError("INVALID_PATH", "拒绝读取符号链接图片")
    if not path.is_file() or path.stat().st_size > max_bytes or path.stat().st_size == 0:
        raise JobError("BAD_IMAGE", "图片缺失或超过大小限制")


def validate_image(path: Path, max_bytes=30 * 1024 * 1024, max_pixels=50_000_000) -> str:
    from PIL import Image

    validate_image_file(path, max_bytes)
    try:
        with Image.open(path) as picture:
            if picture.width * picture.height > max_pixels:
                raise ValueError("too many pixels")
            extension = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(picture.format)
            if not extension:
                raise ValueError("unsupported format")
            picture.verify()
        with Image.open(path) as picture:
            picture.load()
        return extension
    except Exception as exc:
        raise JobError("BAD_IMAGE", "图片无法解码或格式不支持", True) from exc


def identity(manga, chapter):
    return {"source": manga.source, "manga": manga.source_id, "chapter": chapter.source_id,
            "pages": len(chapter.pages)}


def validate_cbz(path, manga, chapter, max_bytes=30 * 1024 * 1024, max_pixels=50_000_000):
    from PIL import Image

    try:
        with zipfile.ZipFile(path) as archive:
            manifest = archive.getinfo("jmemby.json")
            if manifest.file_size > 65536 or json.loads(archive.read(manifest)) != identity(manga, chapter):
                raise ValueError("identity mismatch")
            names = archive.namelist()
            images = [n for n in names if n not in {"ComicInfo.xml", "jmemby.json"}]
            if not images or len(images) != len(chapter.pages) or len(set(names)) != len(names):
                raise ValueError("page mismatch")
            for number, name in enumerate(images, 1):
                if not re.fullmatch(rf"{number:06d}\.(jpg|png|webp)", name):
                    raise ValueError("page order mismatch")
                if archive.getinfo(name).file_size > max_bytes:
                    raise ValueError("page too large")
                raw = archive.read(name)
                with Image.open(io.BytesIO(raw)) as picture:
                    if picture.width * picture.height > max_pixels:
                        raise ValueError("too many pixels")
                    picture.load()
            if archive.getinfo("ComicInfo.xml").file_size > 65536:
                raise ValueError("metadata too large")
            if archive.testzip() is not None:
                raise ValueError("CRC failure")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return path.stat().st_size, digest.hexdigest()
    except Exception as exc:
        raise JobError("INVALID_ARTIFACT", "已有漫画文件无效或身份不匹配，请人工检查") from exc


def comic_info(manga: Manga, chapter: Chapter) -> bytes:
    authors = escape(", ".join(manga.authors), quote=True)
    suffix = f" [JM{manga.source_id}]" if manga.source == "jmcomic" else ""
    title = chapter.title if not suffix or chapter.title.endswith(suffix) else chapter.title + suffix
    series = manga.title if not suffix or manga.title.endswith(suffix) else manga.title + suffix
    xml = f'''<?xml version="1.0" encoding="utf-8"?>
<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Title>{escape(title)}</Title>
  <Series>{escape(series)}</Series>
  <Number>{chapter.ordinal}</Number>
  <Writer>{authors}</Writer>
  <Tags>{escape(", ".join(normalize_tags(manga.tags)))}</Tags>
  <PageCount>{len(chapter.pages)}</PageCount>
</ComicInfo>
'''
    return xml.encode("utf-8")


def staged_pages(staging: Path, number: int):
    """Only exact canonical page filenames; never include SDK scratch files."""
    return [p for extension in (".jpg", ".png", ".webp")
            if (p := staging / f"{number:06d}{extension}").exists() or p.is_symlink()]


def create_cbz(staging: Path, destination: Path, manga: Manga, chapter: Chapter,
               max_bytes=30 * 1024 * 1024, max_pixels=50_000_000) -> None:
    """Create and atomically publish a CBZ from validated staged page files."""
    if not chapter.pages or [p.number for p in chapter.pages] != list(range(1, len(chapter.pages) + 1)):
        raise JobError("BAD_PAGES", "章节为空或页序无效")
    if destination.exists():
        validate_cbz(destination, manga, chapter, max_bytes, max_pixels)
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix="chapter-", suffix=".cbz.part", dir=staging)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            for page in chapter.pages:
                candidates = staged_pages(staging, page.number)
                if len(candidates) != 1:
                    raise JobError("BAD_IMAGE", f"缺少或重复图片 {page.number}")
                extension = validate_image(candidates[0], max_bytes, max_pixels)
                archive.write(candidates[0], f"{page.number:06d}{extension}")
            archive.writestr("ComicInfo.xml", comic_info(manga, chapter))
            archive.writestr("jmemby.json", json.dumps(identity(manga, chapter)))
        validate_cbz(temporary, manga, chapter, max_bytes, max_pixels)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Hard-link publication is atomic and fails if a target already exists.
        # Staging and library must share a filesystem. No partial file is visible.
        try:
            os.link(temporary, destination)
        except FileExistsError:
            validate_cbz(destination, manga, chapter, max_bytes, max_pixels)
    finally:
        temporary.unlink(missing_ok=True)
