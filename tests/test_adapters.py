import importlib.util
import asyncio
import io
from pathlib import Path
import tempfile
import json
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from jmemby.integrations.komga.adapter import ApiError, KomgaAPI
from jmemby.providers.jm_adapter import handle, classify_error, initialize_client, StageError
from jmemby.integrations.komga.bridge import KomgaBridge
from jmemby.domain.errors import JobError


class Photo:
    id, name = "52", "第一章"
    def __len__(self):
        return 2
    def __getitem__(self, index):
        return ("image0", "image1")[index]


class SDK:
    def get_album_detail(self, album_id):
        class Album(list):
            name, authors = "漫画", ["作者"]
        return Album([Photo()])
    def get_photo_detail(self, photo_id):
        return Photo()
    def download_by_image_detail(self, image, path):
        self.image = image
        Path(path).write_bytes(b"decoded")


class AdapterTests(unittest.TestCase):
    def test_pixel_limit_rejects_header_before_sdk_decodes(self):
        from PIL import Image
        stream = io.BytesIO()
        Image.new('RGB', (40, 40)).save(stream, 'PNG')
        content = stream.getvalue()
        class DecodingSDK(SDK):
            loaded = False
            def download_by_image_detail(self, image, path):
                with Image.open(io.BytesIO(content)) as decoded:
                    self.loaded = True
                    decoded.load()
                    decoded.save(path, 'JPEG')
        client = DecodingSDK()
        previous = Image.MAX_IMAGE_PIXELS
        with tempfile.TemporaryDirectory() as directory:
            request = {'operation': 'download_batch', 'pages': [{'number': 1, 'url': 'jm:52:0'}],
                       'directory': directory, 'max_bytes': 10000, 'max_pixels': 1000, 'budget': 10000}
            with self.assertRaises(StageError) as caught:
                handle(request, client)
            self.assertFalse(client.loaded)
            self.assertEqual(classify_error(caught.exception, 'download')['error']['code'], 'IMAGE_LIMIT')
            self.assertEqual(Image.MAX_IMAGE_PIXELS, previous)
            self.assertEqual(list(Path(directory).iterdir()), [])
            request['max_pixels'] = 2000
            self.assertEqual(handle(request, client), {})
            self.assertTrue(client.loaded)
            self.assertTrue((Path(directory) / '000001.jpg').is_file())
            self.assertEqual(Image.MAX_IMAGE_PIXELS, previous)

    def test_memory_and_image_errors_classified_without_private_details(self):
        from PIL import Image
        for error, code in [(MemoryError('private'), 'MEMORY_LIMIT'),
                            (Image.DecompressionBombError('private'), 'IMAGE_LIMIT')]:
            result = classify_error(error, 'image_download_decode')
            self.assertEqual(result['error']['code'], code)
            self.assertNotIn('private', json.dumps(result))

    def test_optional_album_tags(self):
        client = SDK()
        self.assertEqual(handle({"operation": "metadata", "id": "42"}, client)["tags"], [])
        album = client.get_album_detail("42")
        album.tags = ["冒险", " 冒险 ", "剧情", None]
        album.works = ["原作 & 外传", "冒险"]
        album.characters = ["人物甲", "人物甲"]
        with patch.object(client, "get_album_detail", return_value=album):
            self.assertEqual(handle({"operation": "metadata", "id": "42"}, client)["tags"],
                             ["冒险", "剧情", "原作 & 外传", "人物甲"])

    def test_unavailable_tag_field_preserves_works_and_characters(self):
        class Album(list):
            name, authors = "漫画", []
            works, characters = ["原作"], ["人物"]
            @property
            def tags(self):
                raise ValueError("missing metadata")
        with patch.object(SDK, "get_album_detail", return_value=Album([Photo()])):
            self.assertEqual(handle({"operation": "metadata", "id": "42"}, SDK())["tags"], ["原作", "人物"])

    def test_disk_full_sdk_error_is_not_retried_as_network(self):
        import errno
        result = classify_error(OSError(errno.ENOSPC, "full"), "image_download_decode")
        self.assertEqual(result["error"]["code"], "DISK_FULL")

    def test_secure_config_write_ignores_predictable_temp_symlink(self):
        spec = importlib.util.spec_from_file_location("configure_audit", Path(__file__).parents[1] / "scripts/configure.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decoy = root / ".env.install.tmp"
            decoy.write_text("do not overwrite")
            with patch.object(module, "ROOT", root), patch.object(module, "ENV", root / ".env"):
                module.save({"TOKEN": "test"})
            self.assertEqual(decoy.read_text(), "do not overwrite")
            self.assertEqual(module.parse((root / ".env").read_text()), {"TOKEN": "test"})

    def test_batch_reuses_chapter_and_preserves_pages_on_failure(self):
        from PIL import Image
        class BatchSDK(SDK):
            refreshes = 0
            downloads = 0
            fail = True
            def get_photo_detail(self, photo_id):
                self.refreshes += 1
                return Photo()
            def download_by_image_detail(self, image, path):
                self.downloads += 1
                if image == "image1" and self.fail:
                    raise ConnectionError("offline")
                Image.new("RGB", (8, 8)).save(path, "JPEG")
        client = BatchSDK()
        with tempfile.TemporaryDirectory() as root:
            request = {"operation": "download_batch", "pages": [
                {"url": "jm:52:0", "number": 1}, {"url": "jm:52:1", "number": 2}],
                "directory": root, "max_bytes": 10000, "max_pixels": 1000, "budget": 20000}
            with self.assertRaises(StageError):
                handle(request, client)
            self.assertEqual(client.refreshes, 1)
            first = Path(root) / "000001.jpg"
            before = first.read_bytes()
            client.fail = False
            request["pages"] = request["pages"][1:]
            self.assertEqual(handle(request, client), {})
            self.assertEqual(client.refreshes, 2)
            self.assertEqual(client.downloads, 3)
            self.assertEqual(first.read_bytes(), before)
            self.assertTrue((Path(root) / "000002.jpg").exists())

    def test_download_error_identifies_refresh_or_image_stage(self):
        class ResponseUnexpectedException(Exception):
            pass
        request = {"operation": "download", "url": "jm:52:1", "max_bytes": 30}
        with tempfile.TemporaryDirectory() as directory:
            request["destination"] = str(Path(directory) / "image.part")
            for method, phase in (("get_photo_detail", "download_photo_refresh"),
                                  ("download_by_image_detail", "image_download_decode")):
                client = SDK()
                with patch.object(client, method, side_effect=ResponseUnexpectedException("secret body")):
                    with self.assertRaises(StageError) as caught:
                        handle(request, client)
                result = classify_error(caught.exception, "download")
                self.assertEqual(result["error"]["diagnostic"]["phase"], phase)
                self.assertEqual(result["error"]["code"], "JM_RESPONSE_UNEXPECTED")
                self.assertNotIn("secret body", json.dumps(result))

    def test_initialization_unexpected_response_retries_and_recovers(self):
        class ResponseUnexpectedException(Exception):
            pass
        error = ResponseUnexpectedException("secret response")
        error.__context__ = AssertionError("secret")
        self.assertEqual(classify_error(error, "client_init")["error"]["code"], "JM_RESPONSE_UNEXPECTED")
        client = object()
        with patch("jmemby.providers.jm_adapter.make_client", side_effect=[error, client]) as factory, patch("jmemby.providers.jm_adapter.time.sleep"):
            self.assertIs(initialize_client(), client)
            self.assertEqual(factory.call_count, 2)

    def test_initialization_retry_is_bounded_and_config_errors_not_retried(self):
        for error, attempts in ((ConnectionError("offline"), 3), (ValueError("config"), 1)):
            with patch("jmemby.providers.jm_adapter.make_client", side_effect=error) as factory, patch("jmemby.providers.jm_adapter.time.sleep"):
                with self.assertRaises(type(error)):
                    initialize_client()
                self.assertEqual(factory.call_count, attempts)

    def test_source_error_classification_and_redaction(self):
        for error, expected in ((TimeoutError("secret-url"), "TIMEOUT"),
                                (AttributeError("secret-cookie"), "JM_SDK_INCOMPATIBLE"),
                                (ConnectionError("secret-password"), "NETWORK")):
            result = classify_error(error, "metadata")
            self.assertEqual(result["error"]["code"], expected)
            self.assertNotIn("secret", json.dumps(result))
        wrapper = RuntimeError("sensitive SDK message")
        wrapper.__cause__ = TimeoutError("sensitive proxy URL")
        self.assertEqual(classify_error(wrapper, "metadata")["error"]["code"], "TIMEOUT")

    def test_sdk_metadata_and_stable_pages(self):
        client = SDK()
        self.assertEqual(handle({"operation": "metadata", "id": "42"}, client)["chapters"], [{"id": "52", "title": "第一章"}])
        self.assertEqual(handle({"operation": "pages", "chapter_id": "52"}, client)["pages"], ["jm:52:0", "jm:52:1"])

    def test_sdk_decoder_and_size_limit(self):
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / "image.part"
            client = SDK()
            request = {"operation": "download", "url": "jm:52:1", "destination": str(destination), "max_bytes": 30}
            self.assertEqual(handle(request, client), {})
            self.assertEqual(client.image, "image1")
            self.assertEqual(destination.read_bytes(), b"decoded")
            destination.unlink()
            request["max_bytes"] = 1
            self.assertEqual(handle(request, client)["error"]["code"], "SIZE_LIMIT")
            self.assertFalse(destination.exists())
            self.assertFalse(list(Path(root).glob("*.jpg")))

    def test_komga_standalone_scan_validates_library_and_requests_scan(self):
        api = KomgaAPI("http://komga:25600", "user", "secret", "lib")
        calls = []
        def request(method, path):
            calls.append((method, path))
            return {'root': '/data'} if method == 'GET' else None
        api.request = request
        self.assertEqual(api.scan(), {'ok': True})
        self.assertEqual(calls, [('GET','/api/v1/libraries/lib'), ('POST','/api/v1/libraries/lib/scan')])

    def test_komga_pages_and_exact_library_path(self):
        api = KomgaAPI("http://komga:25600", "user", "secret", "lib")
        calls = []
        def request(method, path, query=None):
            calls.append((method, path, query))
            if path == "/api/v1/libraries/lib":
                return {"root": "file:///data"}
            if method == "POST":
                return None
            if query["page"] == 0:
                return {"content": [{"id": "wrong", "url": "/data/A/B.cbz", "libraryId": "other"}], "totalPages": 2}
            return {"content": [{"id": "book", "url": "file:///data/A/B.cbz", "libraryId": "lib"}], "last": True}
        api.request = request
        self.assertEqual(api.import_paths(["A/B.cbz"]), {"books": {"A/B.cbz": "book"}})
        self.assertEqual(calls[-1][2]["page"], 1)

    def test_komga_rejects_root_mismatch_and_traversal(self):
        api = KomgaAPI("http://komga:25600", "user", "secret", "lib")
        api.request = lambda *args: {"root": "/different"}
        with self.assertRaises(ApiError):
            api.check_library()
        for path in ("../x.cbz", "/absolute.cbz", "a\\b.cbz"):
            with self.assertRaises(ValueError):
                api.expected_paths([path])

    def test_komga_scan_acceptance_is_not_completion(self):
        api = KomgaAPI("http://komga:25600", "user", "secret", "lib", timeout=0)
        api.request = lambda method, path: {"root": "/data"} if method == "GET" else None
        with self.assertRaises(ApiError):
            api.import_paths(["A/B.cbz"])

    def test_komga_http_errors_and_no_secret_in_error(self):
        api = KomgaAPI("http://komga:25600", "user", "secret", "lib")
        for status, expected in ((401, "KOMGA_AUTH"), (403, "KOMGA_FORBIDDEN"), (404, "KOMGA_NOT_FOUND"), (429, "RATE_LIMIT"), (503, "SERVER_ERROR")):
            error = HTTPError("http://komga", status, "secret", {"Retry-After": "40"}, io.BytesIO())
            with patch.object(api.opener, "open", side_effect=error):
                with self.assertRaises(ApiError) as caught:
                    api.request("GET", "/api/v1/books")
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn("secret", str(caught.exception))
            if status == 429:
                self.assertEqual(caught.exception.retry_after, 40)

    def test_installer_config_roundtrip(self):
        spec = importlib.util.spec_from_file_location("configure", Path(__file__).parents[1] / "scripts/configure.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        value = "a$B# spaces'and\\slashes"
        self.assertEqual(module.parse("KEY=" + module.encode(value))["KEY"], value)
        with self.assertRaises(ValueError):
            module.encode("bad\nvalue")

    def test_komga_bridge_preserves_actionable_error(self):
        command = json.dumps([sys.executable, "-c", "print('{\"error\":{\"code\":\"KOMGA_AUTH\"}}')"])
        with self.assertRaises(JobError) as caught:
            asyncio.run(KomgaBridge(command).request({"operation": "check"}))
        self.assertEqual(caught.exception.code, "KOMGA_AUTH")
        self.assertIn("401", str(caught.exception))
        self.assertIn("密码", str(caught.exception))

    def test_missing_library_root_is_protocol_error(self):
        api = KomgaAPI("http://komga:25600", "user", "secret", "lib")
        api.request = lambda *args: {"unexpected": "response"}
        with self.assertRaises(ApiError) as caught:
            api.check_library()
        self.assertEqual(caught.exception.code, "KOMGA_PROTOCOL")

    def test_library_404_is_distinguished_from_api_404(self):
        for api_missing in (False, True):
            api = KomgaAPI("http://komga:25600", "user", "secret", "wrong-id")
            def request(method, path):
                if path == "/api/v1/libraries" and not api_missing:
                    return [{"id": "real-id", "root": "/data"}]
                raise ApiError("KOMGA_NOT_FOUND")
            api.request = request
            with self.assertRaises(ApiError) as caught:
                api.check_library()
            self.assertEqual(caught.exception.code, "KOMGA_API_NOT_FOUND" if api_missing else "KOMGA_LIBRARY_NOT_FOUND")

    def test_permission_repair_scopes_ownership_and_preserves_files(self):
        import os
        scripts = Path(__file__).parents[1] / "scripts"
        with patch.object(sys, "path", [str(scripts), *sys.path]):
            spec = importlib.util.spec_from_file_location("repair_permissions", scripts / "repair_permissions.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "tasks.sqlite"
            database.write_bytes(b"preserve")
            with patch.object(module.os, "chown", create=True) as chown, patch.object(module.os, "chmod"):
                module.repair(root, 1000, 1000)
                self.assertEqual(chown.call_count, 2)
                for call in chown.call_args_list:
                    self.assertIn(call.args[0], (root, database))
                    self.assertFalse(call.kwargs["follow_symlinks"])
            self.assertEqual(database.read_bytes(), b"preserve")


if __name__ == "__main__":
    unittest.main()
