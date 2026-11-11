"""Phone 端侧节点接入监听器：发布 /api/phone 端点，同时支持 WebSocket 与 SSE + POST 双向流。"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager, suppress
import json
import logging
from pathlib import Path
import socket
from typing import cast

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.websockets import WebSocketState
from starlette.types import Receive, Scope, Send
import uvicorn

from agent.plugin_composition import Context
from core.common.unix_socket import prepare_unix_socket, socket_alias_path

from .bridge import PhoneBridge, PhoneNodeError, _NodeSession

logger = logging.getLogger(__name__)
HEARTBEAT_SECONDS = 10.0
MIN_SHELL_VERSION = "0.4.0"


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


class _PhoneStreamResponse(StreamingResponse):
    """HTTP 响应拥有节点连接寿命，包括响应体尚未开始时的取消。"""

    def __init__(self, content: AsyncGenerator[bytes, None], bridge: PhoneBridge, session: _NodeSession) -> None:
        super().__init__(
            content, media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
        self._bridge = bridge
        self._session = session

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._bridge.unregister_node(self._session)


def _sse_event(name: str, payload: dict[str, object]) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {name}\ndata: {data}\n\n".encode("utf-8")


def create_phone_app(bridge: PhoneBridge) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    plugin_dir = Path(__file__).resolve().parent

    @app.get("/api/phone/apk")
    async def download_apk() -> FileResponse:
        apk_name = f"akashic-shell-v{MIN_SHELL_VERSION}.apk"
        apk_path = plugin_dir / apk_name
        if not apk_path.is_file():
            raise HTTPException(status_code=404, detail=f"需要 Akashic Shell {MIN_SHELL_VERSION} 或更新版本；兼容的 release APK 尚未打包，请勿安装旧版 APK")
        return FileResponse(
            apk_path,
            media_type="application/vnd.android.package-archive",
            filename=apk_name,
        )

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

    @app.post("/api/phone/stream")
    async def node_sse_stream(request: Request) -> StreamingResponse:
        """供 Android Shell 原生 HttpURLConnection 建立长连接接收 RPC 指令（携带同源 Cookie 与 Origin）。"""
        try:
            hello = await request.json()
        except Exception as error:
            raise HTTPException(status_code=400, detail="无效的 JSON hello 载荷") from error
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            raise HTTPException(status_code=400, detail="首包必须是 type=hello")
        if hello.get("supports_connection_id") is not True:
            raise HTTPException(status_code=400, detail=f"SSE 端侧必须支持 connection_id，请升级 Akashic Shell 至 {MIN_SHELL_VERSION} 或更新版本")

        queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()

        async def send_json(payload: dict[str, object]) -> None:
            await queue.put(payload)

        async def close_transport() -> None:
            await queue.put(None)

        try:
            session = await bridge.register_node(
                cast(dict[str, object], hello),
                send_json=send_json,
                close_transport=close_transport,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        async def events() -> AsyncGenerator[bytes, None]:
            yield _sse_event("welcome", {"device_id": session.device_id, "connection_id": session.connection_id})
            while not session.closed:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield _sse_event("heartbeat", {})
                    continue
                if item is None:
                    break
                event_name = str(item.get("type") or "message")
                yield _sse_event(event_name, item)

        return _PhoneStreamResponse(events(), bridge, session)

    @app.post("/api/phone/reply")
    async def node_http_reply(request: Request) -> JSONResponse:
        """供 Android Shell 回传 RPC 执行结果或实时状态变更。"""
        try:
            frame = await request.json()
        except Exception as error:
            raise HTTPException(status_code=400, detail="无效的 JSON 响应载荷") from error
        if not isinstance(frame, dict):
            raise HTTPException(status_code=400, detail="响应载荷必须是 JSON 对象")
        device_id = str(frame.get("device_id") or "").strip()
        if not device_id:
            raise HTTPException(status_code=400, detail="缺少 device_id")
        connection_id = frame.get("connection_id")
        if not isinstance(connection_id, str) or not connection_id or len(connection_id) > 128:
            raise HTTPException(status_code=409, detail="HTTP 回传必须携带当前 welcome 的 connection_id")
        try:
            accepted = bridge.handle_incoming_for_device(device_id, cast(dict[str, object], frame))
        except PhoneNodeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if not accepted:
            raise HTTPException(status_code=404, detail=f"设备 {device_id!r} 未注册或连接已断开")
        return JSONResponse({"ok": True})

    @app.websocket("/api/phone/ws")
    async def node_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        session = None

        async def send_json(payload: dict[str, object]) -> None:
            """发送帧，并在传输边界将断连转换成桥接错误。"""
            if (
                websocket.client_state is not WebSocketState.CONNECTED
                or websocket.application_state is not WebSocketState.CONNECTED
            ):
                raise PhoneNodeError("WebSocket 连接已断开")
            try:
                await websocket.send_text(json.dumps(payload, ensure_ascii=False))
            except WebSocketDisconnect as exc:
                raise PhoneNodeError(f"WebSocket 发送失败（断连码 {exc.code}）") from exc

        async def close_transport() -> None:
            if websocket.client_state is WebSocketState.CONNECTED:
                with suppress(RuntimeError, OSError, WebSocketDisconnect):
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
            await send_json({"type": "welcome", "device_id": session.device_id, "connection_id": session.connection_id})
            while True:
                raw = await websocket.receive_text()
                frame = json.loads(raw)
                if isinstance(frame, dict):
                    if frame.get("type") == "ping":
                        await send_json({"type": "pong", "ts": frame.get("ts")})
                    else:
                        bridge.handle_incoming(session, cast(dict[str, object], frame))
        except (WebSocketDisconnect, PhoneNodeError, TimeoutError, ValueError, OSError):
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
