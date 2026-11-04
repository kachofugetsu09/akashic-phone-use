"""Phone 端侧节点接入监听器：发布 /api/phone 端点，承接 Android Shell 的 WebSocket 与 HTTP 查询。"""
from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager, suppress
import json
import logging
import socket
from typing import cast

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketState
import uvicorn

from agent.plugin_composition import Context
from core.common.unix_socket import prepare_unix_socket, socket_alias_path

from .bridge import PhoneBridge

logger = logging.getLogger(__name__)


class PhoneListenerServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config) -> None:
        self._exit_loop: asyncio.AbstractEventLoop | None = None
        self._exit_event: asyncio.Event | None = None
        self._exit_requested = False
        super().__init__(config)
        self.ready = asyncio.Event()
        self.servers: list[asyncio.AbstractServer] = []

    @contextmanager
    def capture_signals(self) -> Generator[None]:
        yield

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets)
        self.ready.set()

    @property
    def should_exit(self) -> bool:
        return self._exit_requested

    @should_exit.setter
    def should_exit(self, value: bool) -> None:
        self._exit_requested = value
        loop, event = self._exit_loop, self._exit_event
        if value and loop is not None and event is not None and not loop.is_closed():
            _ = loop.call_soon_threadsafe(event.set)

    async def main_loop(self) -> None:
        self._exit_event = asyncio.Event()
        self._exit_loop = asyncio.get_running_loop()
        counter = 0
        should_exit = await self.on_tick(counter)
        while not should_exit:
            counter = (counter + 1) % 864000
            try:
                _ = await asyncio.wait_for(self._exit_event.wait(), timeout=0.1)
            except TimeoutError:
                pass
            should_exit = await self.on_tick(counter)


def create_phone_app(bridge: PhoneBridge) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/api/phone/devices")
    async def list_devices() -> JSONResponse:
        items = [
            {
                "device_id": d.device_id,
                "device_name": d.device_name,
                "model": d.model,
                "sdk_int": d.sdk_int,
                "shell_version": d.shell_version,
                "display_mode": d.display_mode,
                "virtual_display_id": d.virtual_display_id,
                "own_focus": d.own_focus,
                "steal_top_focus_disabled": d.steal_top_focus_disabled,
                "pip_state": d.pip_state,
                "active_packages": list(d.active_packages),
                "takeover_reason": d.takeover_reason,
            }
            for d in bridge.list_devices()
        ]
        return JSONResponse({"devices": items})

    @app.websocket("/api/phone/ws")
    async def node_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        session = None

        async def send_json(payload: dict[str, object]) -> None:
            if websocket.client_state is WebSocketState.CONNECTED:
                await websocket.send_text(json.dumps(payload, ensure_ascii=False))

        async def close_transport() -> None:
            if websocket.client_state is WebSocketState.CONNECTED:
                with suppress(RuntimeError, OSError):
                    await websocket.close(code=1000)

        try:
            first_raw = await asyncio.wait_for(websocket.receive_text(), timeout=15.0)
            hello = json.loads(first_raw)
            if not isinstance(hello, dict) or hello.get("type") != "hello":
                await websocket.close(code=1008, reason="首帧必须是 hello")
                return
            session = await bridge.register_node(
                cast(dict[str, object], hello),
                send_json=send_json,
                close_transport=close_transport,
            )
            await send_json({"type": "welcome", "device_id": session.device_id})
            while True:
                raw = await websocket.receive_text()
                frame = json.loads(raw)
                if isinstance(frame, dict):
                    if frame.get("type") == "ping":
                        await send_json({"type": "pong", "ts": frame.get("ts")})
                    else:
                        bridge.handle_incoming(session, cast(dict[str, object], frame))
        except (WebSocketDisconnect, TimeoutError, ValueError, OSError):
            pass
        finally:
            if session is not None:
                await bridge.unregister_node(session)

    return app


async def start_phone_listener(ctx: Context, bridge: PhoneBridge) -> None:
    """启动 UDS 监听器并发布 /api/phone 端点；卸载或换代时先撤端点再关连接。"""
    path = socket_alias_path(ctx.runtime.workspace / "runtime", "phone-use.sock")
    app = create_phone_app(bridge)
    server = PhoneListenerServer(
        uvicorn.Config(app, log_level="warning", lifespan="off", timeout_graceful_shutdown=5)
    )
    task: asyncio.Task[None] | None = None

    def setup():
        nonlocal task
        prepare_unix_socket(path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound = False
        try:
            listener.bind(str(path))
            bound = True
            listener.setblocking(False)
            node = path.lstat()
            task = asyncio.create_task(server.serve(sockets=[listener]), name="phone-bridge-server")
        except BaseException:
            listener.close()
            if bound:
                path.unlink(missing_ok=True)
            raise
        running = task
        closed = False

        async def close() -> None:
            nonlocal closed
            if closed:
                return
            await bridge.close_all()
            server.should_exit = True
            try:
                await running
            finally:
                listener.close()
                with suppress(FileNotFoundError):
                    current = path.lstat()
                    if (current.st_dev, current.st_ino) == (node.st_dev, node.st_ino):
                        path.unlink()
                closed = True

        return close

    await ctx.effect(setup, label="phone.listener")
    assert task is not None
    ready = asyncio.create_task(server.ready.wait(), name="phone-listener-ready")
    try:
        await asyncio.wait((ready, task), return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            await task
            raise RuntimeError("Phone listener 在就绪前退出")
        if not server.started:
            raise RuntimeError("Phone listener 未完成启动")
    finally:
        ready.cancel()
        await asyncio.gather(ready, return_exceptions=True)

    await ctx.endpoint(
        "phone",
        protocol="http+unix",
        address=str(path),
        routes=("/api/phone",),
    )
