"""JMComic adapter, executed in an isolated process by CommandProvider."""
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import re
import sys
from importlib.metadata import PackageNotFoundError, version
import traceback
import time
import errno
import tempfile
from jmemby.domain.models import normalize_tags


class StageError(Exception):
    def __init__(self, phase):
        self.phase = phase


def at_stage(phase, operation, *args):
    try:
        return operation(*args)
    except Exception as exc:
        raise StageError(phase) from exc


def classify_error(exc, phase):
    """Return diagnostic structure without SDK messages, URLs or credentials."""
    if isinstance(exc, StageError):
        phase = exc.phase
        exc = exc.__cause__ or exc
    chain, current = [], exc
    while current is not None and len(chain) < 6 and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    names = [type(item).__name__ for item in chain]
    lower = " ".join(names).lower()
    statuses = [getattr(getattr(item, "response", None), "status_code", None) for item in chain]
    status = next((s for s in statuses if isinstance(s, int) and 100 <= s <= 599), None)
    if any(isinstance(item, OSError) and item.errno == errno.ENOSPC for item in chain):
        code = "DISK_FULL"
    elif status == 429:
        code = "RATE_LIMIT"
    elif status in (401, 403):
        code = "JM_ACCESS_DENIED"
    elif status == 404:
        code = "JM_NOT_FOUND"
    elif status and status >= 500:
        code = "SERVER_ERROR"
    elif "timeout" in lower:
        code = "TIMEOUT"
    elif any(name in lower for name in ("connection", "ssl", "proxy", "gaierror")):
        code = "NETWORK"
    elif any(isinstance(item, (ImportError, AttributeError, TypeError)) for item in chain):
        code = "JM_SDK_INCOMPATIBLE"
    elif "missing" in lower:
        code = "JM_NOT_FOUND"
    elif "responseunexpectedexception" in lower:
        code = "JM_RESPONSE_UNEXPECTED"
    elif phase == "client_init":
        code = "JM_CONFIG_ERROR"
    else:
        code = "JM_CLIENT_ERROR"
    try:
        sdk_version = version("jmcomic")
    except PackageNotFoundError:
        sdk_version = "not-installed"
    frames = traceback.extract_tb(exc.__traceback__)[-4:]
    return {"error": {"code": code, "diagnostic": {
        "phase": phase, "exceptions": names, "http_status": status,
        "sdk_version": sdk_version,
        "frames": [f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in frames],
    }}}


def make_client():
    from jmcomic import JmOption
    options = {"log": False, "client": {"impl": "api", "retry_times": 0,
               "postman": {"meta_data": {"timeout": 30}}}}
    domains = [s.strip() for s in os.getenv("JM_API_DOMAINS", "").split(",") if s.strip()]
    if domains:
        options["client"]["domain"] = domains
    return JmOption.construct(options).new_jm_client()


def initialize_client():
    """Retry only client construction, never replay a successful download.

    The SDK's internal retries remain disabled. Initialization has at most three
    attempts per process; the parent operation timeout still bounds total time.
    Auth errors, missing content and invalid configuration are not retried here.
    """
    for attempt in range(3):
        try:
            return make_client()
        except Exception as exc:
            code = classify_error(exc, "client_init")["error"]["code"]
            if attempt == 2 or code not in {"TIMEOUT", "NETWORK", "SERVER_ERROR", "JM_RESPONSE_UNEXPECTED"}:
                raise
            time.sleep(2 ** attempt)


def handle(request, client):
    operation = request["operation"]
    if operation == "search_tag":
        try:
            from jmcomic import JmMagicConstants
            order, period = JmMagicConstants.ORDER_BY_LIKE, JmMagicConstants.TIME_ALL
            search = client.search_tag
        except (ImportError, AttributeError):
            return {"error": {"code": "DISCOVERY_UNSUPPORTED"}}
        if not isinstance(request["tag"], str) or not 1 <= len(request["tag"]) <= 100:
            raise ValueError("Invalid tag")
        page_number = request["page"]
        if type(page_number) is not int or not 1 <= page_number <= 10:
            raise ValueError("Invalid page")
        try:
            page = search(search_query=request["tag"], page=page_number, order_by=order, time=period)
        except (TypeError, NotImplementedError):
            return {"error": {"code": "DISCOVERY_UNSUPPORTED"}}
        ids = [str(i) for i, _ in page.iter_id_title()]
        return {"ids": ids, "last": not ids or page_number >= page.page_count,
                "mode": "SOURCE_LIKES_DESC", "scope": "all", "order": str(order)}
    if operation == "discovery_detail":
        album = at_stage("discovery_detail", client.get_album_detail, request["id"])
        complete, fields = True, {}
        for field in ("tags", "works", "characters"):
            try:
                value = getattr(album, field) if field == "tags" else getattr(album, field, [])
                if (not isinstance(value, (list, tuple)) or len(value) > 1000
                        or any(not isinstance(t, str) or len(t) > 1000 for t in value)):
                    raise ValueError()
                fields[field] = list(value)
            except Exception:
                complete, fields[field] = False, []
        try:
            category = getattr(album, "category", [])
            if isinstance(category, str):
                category = [category]
            if not isinstance(category, (list, tuple)) or any(not isinstance(t, str) for t in category):
                raise ValueError()
            flag = getattr(album, "ai_generated", False)
            if type(flag) is not bool:
                raise ValueError()
        except Exception:
            category, flag, complete = [], False, False
        raw = str(getattr(album, "likes", ""))
        exact = int(raw) if re.fullmatch(r"[0-9]{1,18}", raw) else None
        title = album.name
        if not isinstance(title, str) or not title or len(title) > 10000:
            complete = False
        return {"manga": {"source": "jmcomic", "source_id": request["id"],
                          "title": title, "authors": list(album.authors),
                          "tags": normalize_tags(fields["tags"] + fields["works"] + fields["characters"]),
                          "chapters": [{"source_id": str(p.id), "ordinal": n,
                                        "title": p.name, "pages": []} for n, p in enumerate(album, 1)]},
                "tags": fields["tags"], "markers": fields["works"] + fields["characters"] + list(category),
                "complete": complete, "ai_flag": flag, "likes_raw": raw, "likes_exact": exact}
    if operation == "download_batch":
        from jmemby.storage.cbz import validate_image
        entries = request["pages"]
        parsed = [re.fullmatch(r"jm:([0-9]+):([0-9]+)", entry["url"]) for entry in entries]
        if not entries or any(m is None for m in parsed) or len({m[1] for m in parsed}) != 1:
            raise ValueError("Batch must contain exactly one chapter")
        if any(type(e["number"]) is not int or e["number"] <= 0 for e in entries):
            raise ValueError("Invalid page number")
        photo = at_stage("download_photo_refresh", client.get_photo_detail, parsed[0][1])

        class ChapterClient:
            def get_photo_detail(self, chapter_id):
                return photo

            def download_by_image_detail(self, image, path):
                return client.download_by_image_detail(image, path)

        cached = ChapterClient()
        budget = int(request["budget"])
        for entry in entries:
            number = entry["number"]
            destination = Path(request["directory"]) / f"{number:06d}.part"
            result = handle({"operation": "download", "url": entry["url"],
                             "destination": str(destination), "max_bytes": request["max_bytes"]}, cached)
            if "error" in result:
                return result
            extension = validate_image(destination, request["max_bytes"], request["max_pixels"])
            budget -= destination.stat().st_size
            if budget < 0:
                destination.unlink(missing_ok=True)
                return {"error": {"code": "SIZE_LIMIT"}}
            destination.replace(destination.with_suffix(extension))
        return {}
    if operation == "metadata":
        album = at_stage("album_metadata", client.get_album_detail, request["id"])
        tags = []
        for field in ("tags", "works", "characters"):
            try:
                values = normalize_tags(getattr(album, field, ()))
            except Exception:
                continue  # One unavailable field must not discard the other labels.
            tags.extend(tag for tag in values if tag not in tags)
        tags = normalize_tags(tags)
        return {"title": album.name, "authors": list(album.authors), "tags": list(tags),
                "chapters": [{"id": str(photo.id), "title": photo.name} for photo in album]}
    if operation == "pages":
        photo = at_stage("chapter_pages", client.get_photo_detail, request["chapter_id"])
        return {"pages": [f"jm:{photo.id}:{index}" for index in range(len(photo))]}
    if operation == "download":
        match = re.fullmatch(r"jm:([0-9]+):([0-9]+)", request["url"])
        if not match:
            raise ValueError("Invalid page reference")
        # Resolve fresh signed/CDN URLs for every attempt rather than persisting URLs.
        photo = at_stage("download_photo_refresh", client.get_photo_detail, match[1])
        image = at_stage("download_image_detail", photo.__getitem__, int(match[2]))
        destination = Path(request["destination"])
        limit = int(request["max_bytes"])
        if limit <= 0:
            raise ValueError("Invalid size limit")
        # The SDK performs JM image descrambling. Use an image extension because
        # Pillow-based SDK decoders may infer output format from the path.
        descriptor, decoded_name = tempfile.mkstemp(prefix=".decode-", suffix=".jpg", dir=destination.parent)
        os.close(descriptor)
        decoded = Path(decoded_name)
        try:
            at_stage("image_download_decode", client.download_by_image_detail, image, str(decoded))
            if not decoded.is_file() or not 0 < decoded.stat().st_size <= limit:
                return {"error": {"code": "SIZE_LIMIT"}}
            decoded.replace(destination)
        finally:
            decoded.unlink(missing_ok=True)
        return {}
    raise ValueError("Unsupported operation")


def main():
    phase = "request_parse"
    try:
        request = json.loads(sys.stdin.readline())
        phase = "client_init"
        with redirect_stdout(sys.stderr):
            client = initialize_client()
            phase = request.get("operation", "unknown")
            result = handle(request, client)
    except Exception as exc:
        result = classify_error(exc, phase)
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
