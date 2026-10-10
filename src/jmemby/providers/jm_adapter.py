"""JMComic adapter, executed in an isolated process by CommandProvider."""
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import re
import sys


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
    try:
        request = json.loads(sys.stdin.readline())
        with redirect_stdout(sys.stderr):
            result = handle(request, make_client())
    except (ValueError, KeyError, IndexError, TypeError):
        result = {"error": {"code": "PROVIDER_PROTOCOL"}}
    except Exception as exc:
        # Never emit SDK error bodies, credentials, URLs or cookies.
        name = type(exc).__name__.lower()
        code = "NETWORK" if any(s in name for s in ("timeout", "connection", "request")) else "JM_CLIENT_ERROR"
        result = {"error": {"code": code}}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
