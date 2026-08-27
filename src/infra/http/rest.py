"""Translate FastAPI HTTP messages without changing feature behavior."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from collections.abc import Callable

from fastapi import FastAPI, Request as FastAPIRequest, WebSocket
from starlette.responses import Response as FastAPIResponse
from starlette.websockets import WebSocketDisconnect

from src.shared import Request, Response, WebSocketGateway


_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD")


def create_fastapi_app(
    handler: Callable[[Request], Response],
    websocket: WebSocketGateway | None = None,
    *,
    startup: Callable[[], None] | None = None,
    shutdown: Callable[[], None] | None = None,
) -> FastAPI:
    """Expose one shared request handler through an ASGI HTTP transport."""

    if (startup is None) != (shutdown is None):
        raise ValueError("startup and shutdown must be supplied together")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if startup is not None:
            startup()
        try:
            yield
        finally:
            if shutdown is not None:
                shutdown()

    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    async def dispatch(incoming: FastAPIRequest) -> FastAPIResponse:
        query = incoming.scope.get("query_string", b"").decode("latin-1")
        target = incoming.url.path + ("?" + query if query else "")
        outgoing = await asyncio.to_thread(
            handler,
            Request(
                incoming.method,
                target,
                dict(incoming.headers),
                await incoming.body(),
            ),
        )
        response = FastAPIResponse(outgoing.body, status_code=outgoing.status)
        response.raw_headers = [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in outgoing.headers
        ]
        return response

    app.add_api_route("/", dispatch, methods=list(_METHODS), include_in_schema=False)
    app.add_api_route(
        "/{path:path}", dispatch, methods=list(_METHODS), include_in_schema=False
    )

    if websocket is not None:

        @app.websocket("/ws")
        async def dispatch_websocket(socket: WebSocket) -> None:
            loop = asyncio.get_running_loop()
            outbound: asyncio.Queue[bytes] = asyncio.Queue()

            def enqueue(payload: bytes) -> None:
                try:
                    loop.call_soon_threadsafe(outbound.put_nowait, payload)
                except RuntimeError:
                    pass

            session_id = websocket.open(socket.headers.get("origin"), enqueue)
            if session_id is None:
                await socket.close(code=1008)
                return
            await socket.accept()

            async def send_outbound() -> None:
                while True:
                    payload = await outbound.get()
                    try:
                        await socket.send_text(payload.decode("utf-8"))
                    finally:
                        outbound.task_done()

            sender = asyncio.create_task(send_outbound())
            try:
                while True:
                    message = await socket.receive()
                    if message["type"] == "websocket.disconnect":
                        break
                    payload = message.get("bytes")
                    if payload is None:
                        payload = message.get("text", "").encode("utf-8")
                    result = websocket.receive(session_id, payload)
                    if result.payload is not None:
                        await outbound.put(result.payload)
                    if result.close_code is not None:
                        await outbound.join()
                        await socket.close(code=result.close_code)
                        break
            except WebSocketDisconnect:
                pass
            finally:
                websocket.disconnect(session_id)
                sender.cancel()
                with suppress(asyncio.CancelledError):
                    await sender
    return app
