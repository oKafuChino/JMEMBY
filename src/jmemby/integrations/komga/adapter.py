"""Komga REST integration using Basic authentication and paginated books."""
import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import PurePosixPath
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, unquote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class ApiError(Exception):
    def __init__(self, code, retry_after=0):
        self.code, self.retry_after = code, retry_after


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class KomgaAPI:
    def __init__(self, base, username, password, library_id, root="/data", timeout=240):
        parsed = urlsplit(base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Invalid Komga URL")
        if not username or not password or not library_id:
            raise ValueError("Komga credentials and library ID are required")
        self.base, self.library_id = base.rstrip("/"), library_id
        self.root, self.timeout = PurePosixPath(root), timeout
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise ValueError("Invalid library root")
        self.authorization = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        self.opener = build_opener(NoRedirect)

    def request(self, method, path, query=None):
        url = self.base + path + ("?" + urlencode(query, doseq=True) if query else "")
        req = Request(url, method=method, headers={"Authorization": self.authorization, "Accept": "application/json"})
        try:
            with self.opener.open(req, timeout=30) as response:
                body = response.read(16 * 1024 * 1024 + 1)
                if len(body) > 16 * 1024 * 1024:
                    raise ApiError("KOMGA_PROTOCOL")
                return json.loads(body) if body else None
        except HTTPError as exc:
            if exc.code == 429:
                value = exc.headers.get("Retry-After", "30")
                try:
                    delay = float(value)
                except ValueError:
                    try:
                        delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError):
                        delay = 30
                raise ApiError("RATE_LIMIT", max(0, min(delay, 86400))) from exc
            raise ApiError("SERVER_ERROR" if exc.code >= 500 else "KOMGA_AUTH" if exc.code in (401, 403) else "KOMGA_HTTP") from exc
        except (URLError, TimeoutError) as exc:
            raise ApiError("NETWORK") from exc

    @staticmethod
    def file_path(value):
        if not isinstance(value, str):
            return None
        if value.startswith("file:"):
            uri = urlsplit(value)
            if uri.netloc not in ("", "localhost"):
                return None
            value = unquote(uri.path)
        path = PurePosixPath(value)
        return str(path) if path.is_absolute() and ".." not in path.parts else None

    def expected_paths(self, paths):
        result = {}
        for relative in paths:
            path = PurePosixPath(relative)
            if not relative or path.is_absolute() or ".." in path.parts or "\\" in relative:
                raise ValueError("Invalid relative path")
            result[str(self.root / path)] = relative
        if not result or len(result) != len(paths):
            raise ValueError("Empty or duplicate paths")
        return result

    def check_library(self):
        library = self.request("GET", f"/api/v1/libraries/{quote(self.library_id, safe='')}")
        if not isinstance(library, dict) or self.file_path(library.get("root")) != str(self.root):
            raise ApiError("KOMGA_ROOT_MISMATCH")
        return {"library_id": self.library_id, "root": str(self.root)}

    def import_paths(self, paths):
        expected = self.expected_paths(paths)
        self.check_library()
        self.request("POST", f"/api/v1/libraries/{quote(self.library_id, safe='')}/scan")
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            found, page = {}, 0
            while time.monotonic() < deadline:
                data = self.request("GET", "/api/v1/books", {"library_id": self.library_id, "page": page, "size": 200})
                if not isinstance(data, dict) or not isinstance(data.get("content"), list):
                    raise ApiError("KOMGA_PROTOCOL")
                for book in data["content"]:
                    path = self.file_path(book.get("url"))
                    if path in expected and str(book.get("libraryId")) == self.library_id and not book.get("deleted", False):
                        found[expected[path]] = str(book["id"])
                if data.get("last") is True or page + 1 >= int(data.get("totalPages", 1)):
                    break
                page += 1
            if set(found) == set(paths):
                return {"books": found}
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        raise ApiError("SERVER_ERROR")


def from_env():
    return KomgaAPI(os.getenv("KOMGA_BASE_URL", "http://komga:25600"), os.getenv("KOMGA_USERNAME", ""),
                    os.getenv("KOMGA_PASSWORD", ""), os.getenv("KOMGA_LIBRARY_ID", ""),
                    os.getenv("KOMGA_LIBRARY_ROOT", "/data"), int(os.getenv("KOMGA_IMPORT_TIMEOUT", "240")))


def main():
    try:
        request = json.loads(sys.stdin.readline())
        api = from_env()
        if request["operation"] == "check":
            result = api.check_library()
        elif request["operation"] == "import":
            result = api.import_paths(request["paths"])
        else:
            raise ValueError("Unsupported operation")
    except ApiError as exc:
        result = {"error": {"code": exc.code, "retry_after": exc.retry_after}}
    except Exception:
        result = {"error": {"code": "KOMGA_PROTOCOL"}}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
