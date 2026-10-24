"""JMComic adapter, executed in an isolated process by CommandProvider."""
from contextlib import contextmanager, redirect_stdout
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
import warnings
from jmemby.domain.models import normalize_tags
from jmemby.domain.errors import JobError


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
    job_error = next((item for item in chain if isinstance(item, JobError)), None)
    if job_error is not None:
        code = job_error.code
    elif any(isinstance(item, MemoryError) for item in chain):
        code = "MEMORY_LIMIT"
    elif any(name in ('DecompressionBombWarning', 'DecompressionBombError') for name in names):
        code = "IMAGE_LIMIT"
    elif any(isinstance(item, OSError) and item.errno == errno.ENOSPC for item in chain):
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


def make_client(low_memory=False):
    from jmcomic import JmOption
    options = {"log": False, "client": {"impl": "api", "retry_times": 0,
               "postman": {"meta_data": {"timeout": 30}}}}
    domains = [s.strip() for s in os.getenv("JM_API_DOMAINS", "").split(",") if s.strip()]
    if domains:
        options["client"]["domain"] = domains
    if low_memory:
        # requests iter_content pulls on demand. curl_cffi streaming can queue
        # incoming body chunks in a background thread on a slow disk.
        options['client']['postman']['type'] = 'requests'
    option = JmOption.construct(options)
    if low_memory:
        option.client.postman.meta_data.src_dict.pop('impersonate', None)
    return option.new_jm_client()


def initialize_client(low_memory=False):
    """Retry only client construction, never replay a successful download.

    The SDK's internal retries remain disabled. Initialization has at most three
    attempts per process; the parent operation timeout still bounds total time.
    Auth errors, missing content and invalid configuration are not retried here.
    """
    for attempt in range(3):
        try:
            return make_client(low_memory=True) if low_memory else make_client()
        except Exception as exc:
            code = classify_error(exc, "client_init")["error"]["code"]
            if attempt == 2 or code not in {"TIMEOUT", "NETWORK", "SERVER_ERROR", "JM_RESPONSE_UNEXPECTED"}:
                raise
            time.sleep(2 ** attempt)


@contextmanager
def image_pixel_limit(max_pixels):
    """Reject oversized headers before the SDK allocates decoded pixel buffers."""
    from PIL import Image
    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            yield
    finally:
        Image.MAX_IMAGE_PIXELS = previous


def download_raw(client, image, destination, limit):
    """Stream encoded bytes; do not use SDK wrappers which access resp.content."""
    from jmcomic import JmImageTool, JmModuleConfig
    from urllib.parse import urljoin, urlsplit

    if type(limit) is not int or limit <= 0:
        raise ValueError('Invalid size limit')
    url = image.download_url
    segments = JmImageTool.get_num_by_url(int(image.scramble_id), url.split('?', 1)[0])
    kwargs = {'headers': JmModuleConfig.new_html_headers()}
    # Preserve the API client's image-specific headers and configured transport.
    client.update_request_with_specify_domain(kwargs, None, True)
    kwargs.update(stream=True, allow_redirects=False)
    # requests' automatic redirects read intermediate bodies in full. Follow a
    # bounded number ourselves, closing each unread response before proceeding.
    for redirects in range(6):
        response = client.postman.get(url, **kwargs)
        if response.status_code not in {301, 302, 303, 307, 308}:
            break
        try:
            location = response.headers.get('Location')
            target = urljoin(url, location) if location else ''
            parsed = urlsplit(target)
            if (redirects == 5 or parsed.scheme not in {'http', 'https'} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or (urlsplit(url).scheme == 'https' and parsed.scheme != 'https')):
                raise JobError('NETWORK', 'Invalid image redirect', True)
            url = target
        finally:
            response.close()
    created = False
    try:
        response.raise_for_status()
        if response.status_code != 200:
            raise JobError('NETWORK', 'Unexpected image response', True)
        length = response.headers.get('Content-Length')
        if length is not None and int(length) > limit:
            raise JobError('SIZE_LIMIT', 'Encoded page exceeds byte limit')
        received = 0
        with destination.open('xb') as output:
            created = True
            for chunk in response.iter_content(chunk_size=65536):
                received += len(chunk)
                if received > limit:
                    raise JobError('SIZE_LIMIT', 'Encoded page exceeds byte limit')
                output.write(chunk)
        if not received:
            raise JobError('BAD_IMAGE', 'Empty image response')
        return {'segments': segments}
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    finally:
        response.close()


def handle(request, client):
    operation = request["operation"]
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
                             "destination": str(destination), "max_bytes": request["max_bytes"],
                             "max_pixels": request['max_pixels']}, cached)
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
        # JM exposes featured characters as actors; accept characters from older bridges.
        # Put them first so the tag limit never lets generic tags crowd them out.
        for field in ("actors", "characters", "works", "tags"):
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
    if operation in {"download", "download_raw"}:
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
        if operation == 'download_raw':
            return at_stage('image_transfer', download_raw, client, image, destination, limit)
        # The SDK performs JM image descrambling. Use an image extension because
        # Pillow-based SDK decoders may infer output format from the path.
        descriptor, decoded_name = tempfile.mkstemp(prefix=".decode-", suffix=".jpg", dir=destination.parent)
        os.close(descriptor)
        decoded = Path(decoded_name)
        try:
            pixels = request.get('max_pixels', 50_000_000)
            if type(pixels) is not int or pixels <= 0:
                raise ValueError('Invalid pixel limit')
            with image_pixel_limit(pixels):
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
            client = initialize_client(low_memory=True) if request.get('operation') == 'download_raw' else initialize_client()
            phase = request.get("operation", "unknown")
            result = handle(request, client)
    except Exception as exc:
        result = classify_error(exc, phase)
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
