from __future__ import annotations

import asyncio
import json
import unittest
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

from src.application import VectorShelfApplication
from src.documents import DocumentWorkspace, register_document_routes
from src.documents.testing import MemoryStorage
from src.infra.http import create_fastapi_app
from src.platform import PlatformApp
from src.shared import Principal, Request, Response


async def _call(app, method, target, headers=(), body=b""):
    path, separator, query = target.partition("?")
    sent = []
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode() if separator else b"",
            "headers": [(name.lower().encode(), value.encode()) for name, value in headers],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 80),
            "root_path": "",
        },
        receive,
        send,
    )
    started = next(message for message in sent if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return started["status"], tuple(started["headers"]), response_body


class FastApiRestAdapterTests(unittest.TestCase):
    def test_IT_STAGE16_blocked_database_request_does_not_block_uvicorn_recovery(self):
        release = Event()

        def handler(request):
            if request.path == "/blocked" and not release.wait(1):
                return Response(500)
            release.set()
            return Response(200)

        async def exercise():
            app = create_fastapi_app(handler)
            blocked = asyncio.create_task(_call(app, "GET", "/blocked"))
            await asyncio.sleep(0)
            recovered = await _call(app, "GET", "/recovered")
            return await blocked, recovered

        blocked, recovered = asyncio.run(exercise())
        self.assertEqual((200, 200), (blocked[0], recovered[0]))

    def test_AC_DOC_001_zero_byte_multipart_file_is_rejected(self):
        boundary = "vectorshelf-boundary"
        body = (
            b"--vectorshelf-boundary\r\n"
            b'Content-Disposition: form-data; name="title"\r\n\r\nTitle\r\n'
            b"--vectorshelf-boundary\r\n"
            b'Content-Disposition: form-data; name="visibility"\r\n\r\nPRIVATE\r\n'
            b"--vectorshelf-boundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="empty.txt"\r\n'
            b"Content-Type: text/plain\r\n\r\n"
            b"\r\n--vectorshelf-boundary--\r\n"
        )
        platform = PlatformApp(
            lambda request: Principal("owner@example.com", user_id=1)
        )
        register_document_routes(platform, DocumentWorkspace(MemoryStorage()))

        status, _, raw = asyncio.run(
            _call(
                create_fastapi_app(platform.handle),
                "POST",
                "/api/documents",
                (("Content-Type", f"multipart/form-data; boundary={boundary}"),),
                body,
            )
        )

        self.assertEqual(
            (400, "DOCUMENT-FILE-001"), (status, json.loads(raw)["code"])
        )

    def test_IT_HTTP_001_request_and_response_contract_is_byte_preserving(self):
        observed = []
        payload = b"raw\x00file"

        def handler(request: Request) -> Response:
            observed.append(request)
            return Response(
                206,
                payload,
                (
                    ("Content-Type", "application/octet-stream"),
                    ("Content-Disposition", "attachment; filename=test.bin"),
                    ("Access-Control-Allow-Origin", "http://localhost:3000"),
                ),
            )

        status, headers, body = asyncio.run(
            _call(
                create_fastapi_app(handler),
                "POST",
                "/any/path?page=2",
                (("Content-Type", "application/json"), ("X-Test", "value")),
                b'{"ok":true}',
            )
        )

        self.assertEqual((206, payload), (status, body))
        self.assertEqual(
            (
                (b"Content-Type", b"application/octet-stream"),
                (b"Content-Disposition", b"attachment; filename=test.bin"),
                (b"Access-Control-Allow-Origin", b"http://localhost:3000"),
            ),
            headers,
        )
        request = observed[0]
        self.assertEqual(("POST", "/any/path", {"page": "2"}), (
            request.method, request.path, request.query_params
        ))
        self.assertEqual(("application/json", "value", b'{"ok":true}'), (
            request.header("content-type"), request.header("x-test"), request.body
        ))

    def test_IT_HTTP_002_root_status_and_error_envelope_are_not_rewritten(self):
        raw = json.dumps(
            {"success": False, "status": 401, "code": "COMMON-007"},
            separators=(",", ":"),
        ).encode()
        app = create_fastapi_app(
            lambda request: Response(
                401,
                raw,
                (("Content-Type", "application/json; charset=utf-8"),),
            )
        )

        status, headers, body = asyncio.run(_call(app, "GET", "/"))

        self.assertEqual((401, raw), (status, body))
        self.assertEqual(
            ((b"Content-Type", b"application/json; charset=utf-8"),), headers
        )

    def test_IT_HTTP_004_lifespan_runs_worker_sync_and_retention_once(self):
        with self.assertRaises(ValueError):
            create_fastapi_app(lambda request: Response(404), startup=lambda: None)

        worker = SimpleNamespace(start=Mock(), shutdown=Mock())
        sync = SimpleNamespace(run=Mock(side_effect=lambda stop: stop.wait()))
        retention = SimpleNamespace(
            serve=Mock(side_effect=lambda stop, **kwargs: stop.wait())
        )
        components = SimpleNamespace(
            worker_runtime=worker,
            sync_dispatcher=sync,
            search_history_retention_job=retention,
            search_history_retention_interval_seconds=60.0,
            dashboard_push=None,
        )
        application = VectorShelfApplication(
            None, components, None, None, None, None
        )
        startup = Mock(wraps=application.start)
        shutdown = Mock(wraps=application.stop)
        app = create_fastapi_app(
            lambda request: Response(404),
            startup=startup,
            shutdown=shutdown,
        )

        async def run_lifespan():
            async with app.router.lifespan_context(app):
                self.assertEqual((1, 1), (startup.call_count, worker.start.call_count))
                self.assertEqual(0, shutdown.call_count)

        asyncio.run(run_lifespan())

        self.assertEqual((1, 1), (startup.call_count, shutdown.call_count))
        self.assertEqual((1, 1), (worker.start.call_count, worker.shutdown.call_count))
        self.assertEqual((1, 1), (sync.run.call_count, retention.serve.call_count))
        self.assertFalse(any(thread.is_alive() for thread in application.background_threads))


if __name__ == "__main__":
    unittest.main()
