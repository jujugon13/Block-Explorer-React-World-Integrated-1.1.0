from __future__ import annotations

import json
import socket
import time
import unittest
from threading import Thread
from urllib.request import Request, urlopen

import uvicorn
from websockets.sync.client import connect

from src.application import create_application
from src.infra.http import create_fastapi_app
from src.platform.test_application import NOW, SETTINGS, _components


class UvicornRegressionTests(unittest.TestCase):
    def test_AC_SYS_003_AC_SYS_004_AC_OPS_003_AC_OPS_004_AC_OPS_005_AC_OPS_006_over_uvicorn(self):
        components, bearer = _components()
        user = components.users.create_user(
            "user@example.com", "unused", "User", 1, NOW
        )
        user_bearer = "Bearer " + components.auth.tokens.issue(user.id, user.email, NOW)
        application = create_application(SETTINGS, components)
        app = create_fastapi_app(
            application.handle,
            application.websocket_gateway,
            startup=application.start,
            shutdown=application.stop,
        )
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="warning", lifespan="on")
        )
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        thread = Thread(
            target=server.run,
            kwargs={"sockets": [listener]},
            name="vectorshelf-test-uvicorn",
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(server.started, "uvicorn did not start")

        base = f"http://127.0.0.1:{port}"
        authorized = {"Authorization": bearer}
        routes = (
            ("GET", "/auth/me", authorized, None, 200),
            ("GET", "/departments", {}, None, 200),
            ("GET", "/api/documents", authorized, None, 200),
            ("GET", "/collections", authorized, None, 200),
            ("GET", "/permissions/documents/1/me", authorized, None, 200),
            ("GET", "/admin/indexing-jobs", authorized, None, 200),
            (
                "POST",
                "/api/search",
                {**authorized, "Content-Type": "application/json"},
                json.dumps({"query": "smoke"}).encode(),
                200,
            ),
            ("GET", "/admin/sync/summary", authorized, None, 200),
            ("POST", "/mcp/tokens", authorized, b"", 201),
            ("GET", "/admin/dashboard/summary", authorized, None, 200),
            ("GET", "/ws/info", {}, None, 200),
        )
        observed = []
        try:
            for method, path, headers, body, expected in routes:
                with urlopen(
                    Request(base + path, data=body, headers=headers, method=method),
                    timeout=5,
                ) as response:
                    observed.append((method, path, response.status))
                    self.assertEqual(expected, response.status)
                    self.assertEqual("uvicorn", response.headers["server"])

            with connect(
                f"ws://127.0.0.1:{port}/ws",
                origin="http://localhost:3000",
                open_timeout=5,
            ) as websocket:
                websocket.send("CONNECT\n\n\x00")
                self.assertTrue(websocket.recv(timeout=5).startswith("ERROR\n"))

            with connect(
                f"ws://127.0.0.1:{port}/ws",
                origin="http://localhost:3000",
                open_timeout=5,
            ) as websocket:
                websocket.send(f"CONNECT\nAuthorization:{user_bearer}\n\n\x00")
                self.assertTrue(websocket.recv(timeout=5).startswith("CONNECTED\n"))
                websocket.send(
                    "SUBSCRIBE\nid:user\ndestination:/topic/dashboard\n\n\x00"
                )
                self.assertTrue(websocket.recv(timeout=5).startswith("ERROR\n"))

            with connect(
                f"ws://127.0.0.1:{port}/ws",
                origin="http://localhost:3000",
                open_timeout=5,
            ) as websocket:
                websocket.send(f"CONNECT\nAuthorization:{bearer}\n\n\x00")
                self.assertTrue(websocket.recv(timeout=5).startswith("CONNECTED\n"))
                websocket.send("SEND\ndestination:/topic/dashboard\n\n{}\x00")
                self.assertTrue(websocket.recv(timeout=5).startswith("ERROR\n"))

            with connect(
                f"ws://127.0.0.1:{port}/ws",
                origin="http://localhost:3000",
                open_timeout=5,
            ) as websocket:
                websocket.send(f"CONNECT\nAuthorization:{bearer}\n\n\x00")
                self.assertTrue(websocket.recv(timeout=5).startswith("CONNECTED\n"))
                websocket.send(
                    "SUBSCRIBE\nid:dashboard\ndestination:/topic/dashboard\n\n\x00"
                )
                deadline = time.monotonic() + 5
                while (
                    application.stomp_broker.subscriber_count("/topic/dashboard") != 1
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                self.assertEqual(
                    1,
                    application.stomp_broker.subscriber_count("/topic/dashboard"),
                )
                for _ in range(10):
                    application.components.dashboard_push.state_transition_committed()
                message = websocket.recv(timeout=5)
                self.assertTrue(message.startswith("MESSAGE\n"))
                with self.assertRaises(TimeoutError):
                    websocket.recv(timeout=0.45)
                application.components.dashboard_push.state_transition_rolled_back()
                with self.assertRaises(TimeoutError):
                    websocket.recv(timeout=0.45)
            deadline = time.monotonic() + 5
            while (
                application.stomp_broker.subscriber_count("/topic/dashboard")
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.assertEqual(
                0,
                application.stomp_broker.subscriber_count("/topic/dashboard"),
            )
            print(f"UVICORN server=uvicorn tcp=127.0.0.1:{port}")
            print("UVICORN routes=" + json.dumps(observed, separators=(",", ":")))
            print("UVICORN websocket=SYS004:ERROR,OPS003:ERROR,OPS004:ERROR,OPS005:1,OPS006:0")
        finally:
            server.should_exit = True
            thread.join(5)
            listener.close()
        self.assertFalse(thread.is_alive(), "uvicorn did not stop")


if __name__ == "__main__":
    unittest.main()
