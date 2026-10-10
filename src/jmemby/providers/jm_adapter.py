"""JMComic adapter, executed in an isolated process by CommandProvider."""
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import re
import sys
from importlib.metadata import PackageNotFoundError, version
import traceback


def classify_error(exc, phase):
    """Return diagnostic structure without SDK messages, URLs or credentials."""
    chain, current = [], exc
    while current is not None and len(chain) < 6 and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    names = [type(item).__name__ for item in chain]
    lower = " ".join(names).lower()
    statuses = [getattr(getattr(item, "response", None), "status_code", None) for item in chain]
    status = next((s for s in statuses if isinstance(s, int) and 100 <= s <= 599), None)
    if status == 429:
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


def handle(request, client):
    operation = request["operation"]
    if operation == "metadata":
        album = client.get_album_detail(request["id"])
        return {"title": album.name, "authors": list(album.authors),
                "chapters": [{"id": str(photo.id), "title": photo.name} for photo in album]}
    if operation == "pages":
        photo = client.get_photo_detail(request["chapter_id"])
        return {"pages": [f"jm:{photo.id}:{index}" for index in range(len(photo))]}
    if operation == "download":
        match = re.fullmatch(r"jm:([0-9]+):([0-9]+)", request["url"])
        if not match:
            raise ValueError("Invalid page reference")
        # Resolve fresh signed/CDN URLs for every attempt rather than persisting URLs.
        photo = client.get_photo_detail(match[1])
        image = photo[int(match[2])]
        destination = Path(request["destination"])
        limit = int(request["max_bytes"])
        if limit <= 0:
            raise ValueError("Invalid size limit")
        # The SDK performs JM image descrambling. Use an image extension because
        # Pillow-based SDK decoders may infer output format from the path.
        decoded = destination.with_suffix(".decoded.jpg")
        try:
            client.download_by_image_detail(image, str(decoded))
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
            client = make_client()
            phase = request.get("operation", "unknown")
            result = handle(request, client)
    except Exception as exc:
        result = classify_error(exc, phase)
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
