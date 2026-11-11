"""Android 实际 SSE + HTTP 回传链路的本地集成回归，不代表真机验收。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
from pathlib import Path
import socket
import sys

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import connect

_root = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "phone_sse_pkg", _root / "contract.py", submodule_search_locations=[str(_root)]
)
assert _spec is not None and _spec.loader is not None
_pkg = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _pkg
_spec.loader.exec_module(_pkg)
from phone_sse_pkg import bridge as bridge_module
from phone_sse_pkg.server import PhoneListenerServer, create_phone_app

PhoneBridge = bridge_module.PhoneBridge
PhoneNodeError = bridge_module.PhoneNodeError


@asynccontextmanager
async def phone_server(wrap_app=None):
    """建立真实 HTTP 监听器，各场景使用独立端口并完整关闭。"""
    bridge = PhoneBridge()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    app = create_phone_app(bridge)
    if wrap_app is not None:
        app = wrap_app(app)
    server = PhoneListenerServer(uvicorn.Config(app, log_level="error", lifespan="off"))
    running = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await asyncio.wait_for(server.ready.wait(), 2)
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        async with httpx.AsyncClient(base_url=url, timeout=2) as client:
            yield bridge, client
    finally:
        await bridge.close_all()
        server.should_exit = True
        await asyncio.wait_for(running, 3)
        listener.close()


class SsePeer:
    """像 Android 一样读 SSE，并用独立 HTTP 请求回传捕获的连接身份。"""

    def __init__(self, client, response, device_id, connection_id, lines):
        self.client = client
        self.response = response
        self.device_id = device_id
        self.connection_id = connection_id
        self.lines = lines

    async def next_event(self):
        event = ""
        data = []
        async for line in self.lines:
            if not line and data:
                return event, json.loads("".join(data))
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        return None

    async def request(self):
        while (event := await self.next_event()) is not None:
            if event[0] == "request":
                return event[1]
        raise AssertionError("SSE ended before request")

    async def reply(self, frame):
        return await self.client.post("/api/phone/reply", json={
            "device_id": self.device_id, "connection_id": self.connection_id, **frame
        })

    async def ack(self, request):
        result = await self.reply({"type": "ack", "request_id": request["request_id"]})
        assert result.status_code == 200


@asynccontextmanager
async def sse_peer(client, device_id="sse-peer"):
    """从 welcome 取得本连接 ID，不从全局当前连接替旧线程补 ID。"""
    async with client.stream("POST", "/api/phone/stream", json={
        "type": "hello", "supports_snapshot_ref": True, "device_id": device_id, "supports_ack": True,
        "supports_connection_id": True, "takeover_reason": None,
    }) as response:
        assert response.status_code == 200
        peer = SsePeer(client, response, device_id, None, response.aiter_lines())
        event, welcome = await peer.next_event()
        assert event == "welcome" and welcome["device_id"] == device_id
        peer.connection_id = welcome["connection_id"]
        yield peer


@pytest.mark.asyncio
async def test_sse_old_state_and_old_finally_cannot_change_new_session() -> None:
    async with phone_server() as (bridge, client):
        async with sse_peer(client) as old:
            async with sse_peer(client) as new:
                assert old.connection_id != new.connection_id
                # 旧 stream 的 finally 已退出，不能注销新连接。
                assert await old.next_event() is None
                stale = await old.reply({
                    "type": "state", "takeover_reason": "stale", "pip_state": "needs_you"
                })
                assert stale.status_code == 409
                devices = (await client.get("/api/phone/devices")).json()["devices"]
                assert len(devices) == 1 and devices[0]["takeover_reason"] is None
                current = await new.reply({
                    "type": "state", "takeover_reason": "user-requested", "pip_state": "needs_you"
                })
                assert current.status_code == 200
                assert bridge.list_devices()[0].takeover_reason == "user-requested"


@pytest.mark.asyncio
async def test_legacy_client_and_missing_identity_fail_without_replacing_session() -> None:
    async with phone_server() as (bridge, client):
        async with sse_peer(client) as active:
            rejected = await client.post("/api/phone/stream", json={
                "type": "hello", "device_id": "sse-peer", "supports_ack": True
            })
            assert rejected.status_code == 400
            assert "0.5.0" in rejected.json()["detail"]
            assert bridge._sessions["sse-peer"].connection_id == active.connection_id
            missing = await client.post("/api/phone/reply", json={
                "type": "state", "device_id": "sse-peer", "takeover_reason": "legacy"
            })
            assert missing.status_code == 409
            assert bridge.list_devices()[0].takeover_reason is None


@pytest.mark.asyncio
async def test_apk_download_does_not_fall_back_to_incompatible_binary(tmp_path, monkeypatch) -> None:
    """仅存在旧版真实制品时，下载入口必须明确失败，不能把它当兼容版本。"""
    from phone_sse_pkg import server as server_module
    (tmp_path / "akashic-shell-v0.3.4.apk").write_bytes(
        (_root / "akashic-shell-v0.3.4.apk").read_bytes()
    )
    monkeypatch.setattr(server_module, "__file__", str(tmp_path / "server.py"))
    async with phone_server() as (_, client):
        response = await client.get("/api/phone/apk")
        assert response.status_code == 404
        assert "0.5.0" in response.json()["detail"]
        assert "release APK 尚未打包" in response.json()["detail"]


@pytest.mark.asyncio
async def test_sse_reconnect_rejects_old_replies_and_ignores_duplicate_response() -> None:
    async with phone_server() as (bridge, client):
        async with sse_peer(client) as old:
            first = asyncio.create_task(bridge.rpc(None, "action", {"action": "tap"}))
            old_request = await old.request()
            await old.ack(old_request)
            async with sse_peer(client) as new:
                with pytest.raises(PhoneNodeError, match="结果不确定"):
                    await first
                second = asyncio.create_task(bridge.rpc(None, "observe", {}))
                try:
                    request = await new.request()
                    await new.ack(request)
                    assert (await old.reply({"type": "ack", "request_id": old_request["request_id"]})).status_code == 409
                    late = {"type": "response", "request_id": old_request["request_id"],
                            "ok": True, "result": {"value": "old"},
                            "state": {"takeover_reason": "stale"}}
                    assert (await old.reply(late)).status_code == 409
                    # 同一当前连接的重复/迟到结果不覆盖另一条在途请求的状态。
                    assert (await new.reply(late)).status_code == 200
                    assert not second.done()
                    assert bridge.list_devices()[0].takeover_reason is None
                    response = {"type": "response", "request_id": request["request_id"],
                                "ok": True, "result": {"value": "current"}}
                    assert (await new.reply(response)).status_code == 200
                    assert await second == {"value": "current"}
                    response["state"] = {"takeover_reason": "duplicate"}
                    assert (await new.reply(response)).status_code == 200
                    assert bridge.list_devices()[0].takeover_reason is None
                finally:
                    second.cancel()
                    await asyncio.gather(second, return_exceptions=True)


@pytest.mark.asyncio
async def test_sse_missing_ack_never_replays_after_reconnect(monkeypatch) -> None:
    monkeypatch.setattr(bridge_module, "ACK_TIMEOUT_S", 0.1)
    async with phone_server() as (bridge, client):
        async with sse_peer(client) as old:
            pending = asyncio.create_task(bridge.rpc(None, "action", {"action": "tap"}))
            await old.request()
            with pytest.raises(PhoneNodeError, match="结果不确定.*未自动重试"):
                await pending
            assert await old.next_event() is None
        async with sse_peer(client) as new:
            pending = asyncio.create_task(bridge.rpc(None, "observe", {}))
            request = await new.request()
            assert request["op"] == "observe"
            assert (await new.reply({"type": "response", "request_id": request["request_id"],
                                     "ok": True, "result": {}})).status_code == 200
            assert await pending == {}


@pytest.mark.asyncio
async def test_sse_queued_deadline_and_cancel_cleanup() -> None:
    async with phone_server() as (bridge, client):
        async with sse_peer(client) as peer:
            first = asyncio.create_task(bridge.rpc(None, "action", {"action": "tap"}))
            request = await peer.request()
            await peer.ack(request)
            try:
                with pytest.raises(PhoneNodeError, match="排队超时.*未发送"):
                    await bridge.rpc(None, "action", {"action": "type_text"}, timeout_ms=100)
            finally:
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
            session = bridge._sessions["sse-peer"]
            assert not session.pending and not session.acks
            next_call = asyncio.create_task(bridge.rpc(None, "observe", {}))
            try:
                next_request = await peer.request()
                assert next_request["op"] == "observe"
                # 取消不证明动作没执行，但迟到结果不能污染下一次调用。
                assert (await peer.reply({"type": "response", "request_id": request["request_id"],
                    "ok": True, "state": {"takeover_reason": "cancelled-old"}})).status_code == 200
                assert not next_call.done() and bridge.list_devices()[0].takeover_reason is None
                assert (await peer.reply({"type": "response", "request_id": next_request["request_id"],
                    "ok": True, "result": {}})).status_code == 200
                assert await next_call == {}
                assert not session.pending and not session.acks
            finally:
                next_call.cancel()
                await asyncio.gather(next_call, return_exceptions=True)
        async with asyncio.timeout(2):
            while (await client.get("/api/phone/devices")).json()["devices"]:
                pass
        assert not bridge.list_devices()


@pytest.mark.asyncio
async def test_sse_disconnect_before_headers_does_not_leak_registered_session() -> None:
    """连接已注册但响应体尚未开始时，客户端取消仍必须结束该会话。"""
    headers_waiting = asyncio.Event()
    finished = asyncio.Event()

    def delay_headers(app):
        async def wrapped(scope, receive, send):
            async def send_frame(message):
                if message["type"] == "http.response.start":
                    headers_waiting.set()
                    await asyncio.Event().wait()
                await send(message)
            try:
                await app(scope, receive, send_frame)
            finally:
                finished.set()
        return wrapped

    async with phone_server(delay_headers) as (bridge, client):
        request = client.build_request("POST", "/api/phone/stream", json={
            "type": "hello", "supports_snapshot_ref": True, "device_id": "sse-peer", "supports_connection_id": True
        })
        connecting = asyncio.create_task(client.send(request, stream=True))
        try:
            await asyncio.wait_for(headers_waiting.wait(), 2)
            assert len(bridge.list_devices()) == 1
            connecting.cancel()
            await asyncio.gather(connecting, return_exceptions=True)
            await asyncio.wait_for(finished.wait(), 2)
            assert not bridge.list_devices()
        finally:
            connecting.cancel()
            await asyncio.gather(connecting, return_exceptions=True)


@pytest.mark.asyncio
async def test_websocket_write_backpressure_respects_rpc_deadline() -> None:
    """实际 ASGI 写入受阻时，总期限不能只约束写入后的 ACK 等待。"""
    write_started = asyncio.Event()
    release_write = asyncio.Event()
    expired_before_release = []
    received = []

    def delay_request_write(app):
        async def wrapped(scope, receive, send):
            async def send_frame(message):
                if message["type"] == "websocket.send" and json.loads(message["text"]).get("type") == "request":
                    write_started.set()
                    await release_write.wait()
                await send(message)
            await app(scope, receive, send_frame)
        return wrapped

    async with phone_server(delay_request_write) as (bridge, client):
        url = str(client.base_url).replace("http://", "ws://").rstrip("/") + "/api/phone/ws"
        async with connect(url) as ws:
            await ws.send(json.dumps({"type": "hello", "supports_snapshot_ref": True, "device_id": "ws-peer", "supports_ack": True}))
            assert json.loads(await ws.recv())["type"] == "welcome"

            async def reply():
                frame = json.loads(await ws.recv())
                received.append(frame["request_id"])
                await ws.send(json.dumps({"type": "ack", "request_id": frame["request_id"]}))
                await ws.send(json.dumps({"type": "response", "request_id": frame["request_id"],
                                          "ok": True, "result": {"received": True}}))

            peer = asyncio.create_task(reply())
            pending = asyncio.create_task(bridge.rpc(None, "action", {
                "action": "type_text", "text": "x" * 16384
            }, timeout_ms=100))
            await asyncio.wait_for(write_started.wait(), 2)

            def release():
                expired_before_release.append(pending.done())
                release_write.set()

            timer = asyncio.get_running_loop().call_later(0.3, release)
            try:
                result = (await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 2))[0]
                await asyncio.wait_for(release_write.wait(), 2)
                assert expired_before_release == [True], {"received": len(received), "result": result}
                assert isinstance(result, PhoneNodeError) and "结果不确定" in str(result)
                assert "发送请求超时" in str(result)
                assert not received
            finally:
                timer.cancel()
                peer.cancel()
                pending.cancel()
                await asyncio.gather(peer, pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_ack_wait_does_not_grant_result_a_new_timeout_budget() -> None:
    """ACK 消耗原有预算，结果不能借最小等待窗口延长整个请求期限。"""
    async with phone_server() as (bridge, client):
        async with sse_peer(client) as peer:
            ack_ready = asyncio.Event()
            response_ready = asyncio.Event()
            pending = asyncio.create_task(bridge.rpc(None, "action", {"action": "tap"}, timeout_ms=100))
            request = await peer.request()
            loop = asyncio.get_running_loop()
            timers = (loop.call_later(0.05, ack_ready.set), loop.call_later(0.12, response_ready.set))
            try:
                await ack_ready.wait()
                await peer.ack(request)
                await response_ready.wait()
                assert (await peer.reply({"type": "response", "request_id": request["request_id"],
                                          "ok": True, "result": {"late": True}})).status_code == 200
                with pytest.raises(PhoneNodeError, match="结果不确定.*超时"):
                    await pending
                assert not bridge._sessions["sse-peer"].pending
                assert not bridge._sessions["sse-peer"].acks
            finally:
                for timer in timers:
                    timer.cancel()
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["headers", "body"])
async def test_sse_transport_timeout_never_claims_definitely_unsent(stage) -> None:
    """进入传输后缺少送达证据，头/正文阻塞也只能报告结果未知。"""
    blocked = asyncio.Event()
    release = asyncio.Event()

    def delay_stream(app):
        async def wrapped(scope, receive, send):
            async def send_frame(message):
                should_block = (
                    stage == "headers" and message["type"] == "http.response.start"
                    or stage == "body" and message["type"] == "http.response.body"
                    and b"event: request" in message.get("body", b"")
                )
                if should_block:
                    blocked.set()
                    await release.wait()
                await send(message)
            await app(scope, receive, send_frame)
        return wrapped

    async with phone_server(delay_stream) as (bridge, client):
        request = client.build_request("POST", "/api/phone/stream", json={
            "type": "hello", "supports_snapshot_ref": True, "device_id": "sse-peer", "supports_ack": True,
            "supports_connection_id": True,
        })
        connecting = asyncio.create_task(client.send(request, stream=True))
        response = None
        try:
            if stage == "headers":
                await asyncio.wait_for(blocked.wait(), 2)
            else:
                response = await connecting
                peer = SsePeer(client, response, "sse-peer", None, response.aiter_lines())
                assert (await peer.next_event())[0] == "welcome"
            pending = asyncio.create_task(bridge.rpc(None, "action", {"action": "tap"}, timeout_ms=100))
            try:
                await asyncio.wait_for(blocked.wait(), 2)
                with pytest.raises(PhoneNodeError, match="结果不确定，可能已执行") as error:
                    await pending
                assert "未发送" not in str(error.value)
                assert "未自动重试" in str(error.value)
                assert not bridge.list_devices()
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        finally:
            connecting.cancel()
            await asyncio.gather(connecting, return_exceptions=True)
            if response is not None:
                await response.aclose()
            release.set()


@pytest.mark.asyncio
async def test_unacked_websocket_close_backpressure_does_not_hold_rpc_or_shutdown(monkeypatch, caplog) -> None:
    """逻辑下线与物理 close 分开；背压不能阻塞 RPC 或插件关闭。"""
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    def block_close(app):
        async def wrapped(scope, receive, send):
            async def send_frame(message):
                if message["type"] == "websocket.close":
                    close_started.set()
                    await release_close.wait()
                await send(message)
            await app(scope, receive, send_frame)
        return wrapped

    monkeypatch.setattr(bridge_module, "CLOSE_TIMEOUT_S", 0.1, raising=False)
    async with phone_server(block_close) as (bridge, client):
        url = str(client.base_url).replace("http://", "ws://").rstrip("/") + "/api/phone/ws"
        async with connect(url) as ws:
            await ws.send(json.dumps({"type": "hello", "supports_snapshot_ref": True, "device_id": "sse-peer", "supports_ack": True}))
            await ws.recv()
            pending = asyncio.create_task(bridge.rpc(None, "action", {"action": "tap"}, timeout_ms=100))
            await ws.recv()  # 请求到达，但端侧不回 ACK。
            await asyncio.wait_for(close_started.wait(), 2)
            try:
                # close 仍受阻时，RPC 必须已按原期限报告未知效果。
                with pytest.raises(PhoneNodeError, match="结果不确定"):
                    await asyncio.wait_for(asyncio.shield(pending), 0.2)
                async with sse_peer(client) as replacement:
                    assert replacement.connection_id
                    assert len(bridge.list_devices()) == 1
                    await asyncio.wait_for(bridge.close_all(), 1)
                    assert not release_close.is_set()
                    assert not bridge.list_devices()
                    assert not bridge._closing_transports
                    assert "phone.transport.close_unconfirmed" in caplog.text
            finally:
                release_close.set()
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_first", [False, True])
async def test_latest_sse_registration_wins_while_previous_websocket_close_is_blocked(cancel_first) -> None:
    """旧 WS 关闭受阻时，两个真实 SSE 注册不能反向覆盖或留下孤儿。"""
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    first_finished = asyncio.Event()
    stream_count = 0

    def block_old_close(app):
        async def wrapped(scope, receive, send):
            nonlocal stream_count
            is_first = False
            if scope["type"] == "http" and scope["path"] == "/api/phone/stream":
                stream_count += 1
                is_first = stream_count == 1

            async def send_frame(message):
                if message["type"] == "websocket.close":
                    close_started.set()
                    await release_close.wait()
                await send(message)
            try:
                await app(scope, receive, send_frame)
            finally:
                if is_first:
                    first_finished.set()
        return wrapped

    async with phone_server(block_old_close) as (bridge, client):
        url = str(client.base_url).replace("http://", "ws://").rstrip("/") + "/api/phone/ws"
        async with connect(url) as old:
            await old.send(json.dumps({"type": "hello", "supports_snapshot_ref": True, "device_id": "sse-peer"}))
            await old.recv()
            request = client.build_request("POST", "/api/phone/stream", json={
                "type": "hello", "supports_snapshot_ref": True, "device_id": "sse-peer", "device_name": "first",
                "supports_ack": True, "supports_connection_id": True,
            })
            first = asyncio.create_task(client.send(request, stream=True))
            try:
                await asyncio.wait_for(close_started.wait(), 2)
                async with sse_peer(client) as second:
                    if cancel_first:
                        first.cancel()
                        result = (await asyncio.gather(first, return_exceptions=True))[0]
                        if isinstance(result, httpx.Response):
                            await result.aclose()
                    release_close.set()
                    if cancel_first:
                        await asyncio.wait_for(first_finished.wait(), 2)
                    else:
                        response = await asyncio.wait_for(first, 2)
                        assert response.status_code == 200
                        first_peer = SsePeer(client, response, "sse-peer", None, response.aiter_lines())
                        assert (await first_peer.next_event())[0] == "welcome"
                    assert bridge._sessions["sse-peer"].connection_id == second.connection_id
                    if not cancel_first:
                        assert await first_peer.next_event() is None
                    pending = asyncio.create_task(bridge.rpc(None, "observe", {}))
                    try:
                        current = await second.request()
                        assert (await second.reply({"type": "response", "request_id": current["request_id"],
                            "ok": True, "result": {"winner": "second"}})).status_code == 200
                        assert await pending == {"winner": "second"}
                    finally:
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
            finally:
                release_close.set()
                first.cancel()
                result = (await asyncio.gather(first, return_exceptions=True))[0]
                if isinstance(result, httpx.Response):
                    await result.aclose()


@pytest.mark.asyncio
async def test_missing_snapshot_capability_does_not_replace_current_connection() -> None:
    """旧 Shell 注册在替换连接前明确失败，不能踢掉已配套升级的设备。"""
    async with phone_server() as (bridge, client):
        async with sse_peer(client) as active:
            rejected = await client.post("/api/phone/stream", json={
                "type": "hello", "device_id": "sse-peer", "supports_connection_id": True,
            })
            assert rejected.status_code == 400
            assert "snapshot_ref" in rejected.json()["detail"]
            assert "0.5.0" in rejected.json()["detail"]
            assert bridge._sessions["sse-peer"].connection_id == active.connection_id
