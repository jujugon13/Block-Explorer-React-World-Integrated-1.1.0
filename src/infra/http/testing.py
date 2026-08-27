"""Actual TCP client used only by HTTP acceptance tests."""

from __future__ import annotations

import http.client
import socket
import time
from threading import Thread
from urllib.parse import urlencode

import uvicorn

from src.shared import Request, Response

from .rest import create_fastapi_app


def request_over_uvicorn(handler, request: Request) -> Response:
    app = create_fastapi_app(handler)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    thread = Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="vectorshelf-ac-uvicorn",
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.005)
    if not server.started:
        raise AssertionError("uvicorn did not start")

    query = urlencode(request.query_params)
    target = request.path + ("?" + query if query else "")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(
            request.method,
            target,
            body=request.body,
            headers=dict(request.headers),
        )
        incoming = connection.getresponse()
        body = incoming.read()
        if incoming.getheader("server") != "uvicorn":
            raise AssertionError("response did not pass through uvicorn")
        headers = tuple(
            (name, value)
            for name, value in incoming.getheaders()
            if name.lower() not in {"date", "server"}
        )
        return Response(incoming.status, body, headers)
    finally:
        connection.close()
        server.should_exit = True
        thread.join(5)
        listener.close()
        if thread.is_alive():
            raise AssertionError("uvicorn did not stop")
