"""通过真实本地 WebSocket 注入丢包和断连，不代表 Android 真机验收。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
from pathlib import Path
import socket
import sys

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

# 外置插件目录不是 Python 包名，沿用现有测试的源码包加载方式。
_root = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "phone_rpc_pkg", _root / "contract.py", submodule_search_locations=[str(_root)]
)
assert _spec is not None and _spec.loader is not None
_pkg = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _pkg
_spec.loader.exec_module(_pkg)
from phone_rpc_pkg import bridge as bridge_module
from phone_rpc_pkg.safety import format_screen_snapshot

PhoneBridge = bridge_module.PhoneBridge
PhoneNodeError = bridge_module.PhoneNodeError


class ProtocolPeer:
    """记录实际收到的请求，并按指定故障丢弃确认或结果。"""

    def __init__(self, bridge, fault: str, screen: dict | None = None) -> None:
        self.bridge = bridge
        self.fault = fault
        self.requests: list[dict] = []
        self.connections = asyncio.Queue()
        self.old_id: str | None = None
        self.stale_sent = asyncio.Event()
        self.release_response = asyncio.Event()
        self.first_received = asyncio.Event()
        self.screen = screen
        self.held_ack = None

    async def accept(self, ws) -> None:
        """将真实 WebSocket 连接接到被测桥接的会话边界。"""
        hello = json.loads(await ws.recv())

        async def send(frame):
            await ws.send(json.dumps(frame))

        session = await self.bridge.register_node(
            hello, send_json=send, close_transport=ws.close
        )
        await ws.send(json.dumps({"type": "welcome"}))
        try:
            async for raw in ws:
                frame = json.loads(raw)
                if self.fault == "late_ack" and frame.get("type") == "ack":
                    self.held_ack = (session, frame)
                else:
                    self.bridge.handle_incoming(session, frame)
        finally:
            await self.bridge.unregister_node(session)

    async def run(self, url: str) -> None:
        """断连后重新注册，保留请求记录以发现自动重放。"""
        while True:
            async with connect(url) as ws:
                await ws.send(json.dumps({
                    "type": "hello", "supports_snapshot_ref": True, "device_id": "fault-peer", "supports_ack": True
                }))
                await ws.recv()
                await self.connections.put(ws)
                async for raw in ws:
                    frame = json.loads(raw)
                    self.requests.append(frame)
                    request_id = frame["request_id"]
                    if len(self.requests) == 1:
                        self.first_received.set()
                        self.old_id = request_id
                        if self.fault == "unacked":
                            continue
                        if self.fault == "disconnect":
                            await ws.close()
                            continue
                    if not (self.fault == "stale" and len(self.requests) == 2):
                        await ws.send(json.dumps({"type": "ack", "request_id": request_id}))
                    if self.fault == "timeout":
                        continue
                    if self.fault == "late_ack":
                        await self.release_response.wait()
                    if self.fault == "block_first" and len(self.requests) == 1:
                        await self.release_response.wait()
                    if self.screen is not None:
                        action = frame.get("payload", {}).get("action")
                        if action == "hand_to_user":
                            self.screen["takeover_reason"] = frame["payload"]["reason"]
                            self.screen["pip_state"] = "needs_you"
                        elif action == "resume_from_user":
                            self.screen["takeover_reason"] = None
                            self.screen["pip_state"] = "running"
                        if self.fault != "screen_without_ref":
                            self.screen["snapshot_ref"] = request_id
                        result = (
                            {"message": "peer received action", "screen": dict(self.screen)}
                            if frame["op"] == "action" else dict(self.screen)
                        )
                        await self.reply(ws, request_id, result)
                        continue
                    if self.fault == "stale" and len(self.requests) == 2:
                        await ws.send(json.dumps({
                            "type": "response", "request_id": self.old_id,
                            "ok": True, "result": {"source": "old"},
                            "state": {"takeover_reason": "stale-takeover", "pip_state": "needs_you"}
                        }))
                        # 等同一连接的下一帧往返，确保旧回包已到达桥接。
                        await ws.send(json.dumps({"type": "ack", "request_id": request_id}))
                        self.stale_sent.set()
                        await self.release_response.wait()
                    if self.fault == "rejected":
                        await ws.send(json.dumps({
                            "type": "response", "request_id": request_id,
                            "ok": False, "error": "STALE_NODE"
                        }))
                    else:
                        await self.reply(ws, request_id, {"source": "current"})

    async def reply(self, ws, request_id, result) -> None:
        await ws.send(json.dumps({
            "type": "response", "request_id": request_id, "ok": True, "result": result
        }))


@asynccontextmanager
async def peer_connection(fault: str, screen: dict | None = None):
    """为每个场景使用独立端口和会话，退出时完整关闭。"""
    bridge = PhoneBridge()
    peer = ProtocolPeer(bridge, fault, screen)
    async with serve(peer.accept, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        task = asyncio.create_task(peer.run(f"ws://127.0.0.1:{port}"))
        try:
            await asyncio.wait_for(peer.connections.get(), 2)
            yield bridge, peer
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await bridge.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["action", "finish", "observe"])
async def test_missing_ack_never_replays_on_reconnect(monkeypatch, op) -> None:
    monkeypatch.setattr(bridge_module, "ACK_TIMEOUT_S", 0.1)
    async with peer_connection("unacked") as (bridge, peer):
        with pytest.raises(PhoneNodeError, match="结果不确定，可能已执行.*未自动重试"):
            await bridge.rpc(None, op, {"action": "tap"}, timeout_ms=3000)
        await asyncio.wait_for(peer.connections.get(), 2)
        assert len(peer.requests) == 1
        # 重连后由调用方显式发起恢复观察，不暗中重复先前请求。
        assert await bridge.rpc(None, "observe", {}) == {"source": "current"}
        assert [frame["op"] for frame in peer.requests] == [op, "observe"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["timeout", "disconnect"])
async def test_result_loss_reports_unknown_outcome(fault) -> None:
    async with peer_connection(fault) as (bridge, peer):
        with pytest.raises(PhoneNodeError, match="结果不确定，可能已执行.*phone_observe"):
            await bridge.rpc(None, "action", {"action": "type_text"}, timeout_ms=150)
        assert len(peer.requests) == 1


@pytest.mark.asyncio
async def test_explicit_failure_is_not_unknown_outcome() -> None:
    async with peer_connection("rejected") as (bridge, peer):
        with pytest.raises(PhoneNodeError, match="^STALE_NODE$"):
            await bridge.rpc(None, "action", {"action": "tap"})
        assert len(peer.requests) == 1


@pytest.mark.asyncio
async def test_old_response_does_not_complete_next_request() -> None:
    async with peer_connection("stale") as (bridge, peer):
        assert await bridge.rpc(None, "observe", {}) == {"source": "current"}
        # 旧会话断开后，同一设备重新注册。
        await bridge.close_all()
        await asyncio.wait_for(peer.connections.get(), 2)
        pending = asyncio.create_task(bridge.rpc(None, "observe", {}))
        try:
            await asyncio.wait_for(peer.stale_sent.wait(), 2)
            # 等桥接收到新请求的 ACK，保证更早发送的旧回包已经处理。
            request_id = peer.requests[-1]["request_id"]
            await asyncio.wait_for(bridge._sessions["fault-peer"].acks[request_id].wait(), 2)
            assert not pending.done()
            assert bridge.list_devices()[0].takeover_reason is None
            assert bridge.list_devices()[0].pip_state == "hidden"
            peer.release_response.set()
            assert await pending == {"source": "current"}
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_request_ids_survive_bridge_restart_without_reuse() -> None:
    ids = []
    for _ in range(2):
        async with peer_connection("success") as (bridge, peer):
            await bridge.rpc(None, "observe", {})
            ids.append(peer.requests[0]["request_id"])
    assert ids[0] != ids[1]


@pytest.mark.asyncio
async def test_expired_queued_action_never_reaches_peer() -> None:
    """第一条动作阻塞到第二条截止时间之后，第二条必须在队列中失败。"""
    async with peer_connection("block_first") as (bridge, peer):
        first = asyncio.create_task(bridge.rpc(None, "action", {"action": "tap"}))
        await asyncio.wait_for(peer.first_received.wait(), 2)
        second = asyncio.create_task(bridge.rpc(
            None, "action", {"action": "type_text", "text": "must-not-send"}, timeout_ms=100
        ))
        # 真实时限到期后才释放第一条响应，直接检查 peer 收到的请求。
        expired_before_release = []

        def release_first():
            expired_before_release.append(second.done())
            peer.release_response.set()

        release = asyncio.get_running_loop().call_later(0.3, release_first)
        try:
            result = await asyncio.wait_for(
                asyncio.gather(first, second, return_exceptions=True), 2
            )
            assert result[0] == {"source": "current"}
            assert len(peer.requests) == 1, peer.requests
            assert isinstance(result[1], PhoneNodeError)
            assert "未发送" in str(result[1])
            assert "结果不确定" not in str(result[1])
            assert expired_before_release == [True]
            # 排队超时不应锁死设备或淘汰仍在工作的连接。
            assert await bridge.rpc(None, "observe", {}) == {"source": "current"}
            assert [frame["op"] for frame in peer.requests] == ["action", "observe"]
        finally:
            release.cancel()
            first.cancel()
            second.cancel()
            await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("package,activity,text", [
    ("com.example.fixture", "VerifyActivity", "短信验证码"),
    ("com.example.fixture", "MainActivity", "确认支付"),
    ("com.example.fixture", "PasswordActivity", "输入支付密码"),
    ("com.example.wallet", "MainActivity", "请输入验证码"),
])
async def test_authorized_content_does_not_force_takeover(package, activity, text) -> None:
    """授权页面的内容不是接管指令；检查普通点击确实到达协议 peer。"""
    screen = {
        "package_name": package, "activity_name": activity,
        "nodes": [{"node_id": 1, "text": text, "bounds": [0, 0, 40, 40], "clickable": True}],
    }
    async with peer_connection("screen", screen) as (bridge, peer):
        observed = await bridge.observe(None)
        _, after = await bridge.perform_action(None, {"action": "tap", "node_id": 1})
        assert observed.takeover_reason is None
        assert observed.nodes[0].text == text
        assert after is not None and after.takeover_reason is None
        assert [frame["op"] for frame in peer.requests] == ["observe", "action"]


@pytest.mark.asyncio
async def test_secure_window_reports_capture_limit_without_forcing_takeover() -> None:
    screen = {"secure_window": True, "nodes": []}
    async with peer_connection("screen", screen) as (bridge, peer):
        observed = await bridge.observe(None)
        await bridge.perform_action(None, {"action": "tap", "x": 20, "y": 20})
        rendered = format_screen_snapshot(observed)
        assert observed.secure_window is True
        assert observed.screenshot_png is None
        assert "FLAG_SECURE" in rendered and "截图可能受限" in rendered
        assert "TAKEOVER REQUIRED" not in rendered
        assert len(peer.requests) == 2


@pytest.mark.asyncio
async def test_explicit_takeover_survives_omitted_field_until_resume() -> None:
    """省略字段不能撤销已确认接管；端侧明确清除后才恢复普通动作。"""
    screen = {"nodes": []}
    async with peer_connection("screen", screen) as (bridge, peer):
        _, held = await bridge.perform_action(None, {
            "action": "hand_to_user", "reason": "请用户检查当前选项"
        })
        assert held is not None and held.takeover_reason == "请用户检查当前选项"
        with pytest.raises(PhoneNodeError, match="等待用户主屏接管"):
            await bridge.perform_action(None, {"action": "tap", "x": 20, "y": 20})
        assert len(peer.requests) == 1
        screen.pop("takeover_reason")
        observed = await bridge.observe(None)
        assert observed.takeover_reason == "请用户检查当前选项"
        with pytest.raises(PhoneNodeError, match="等待用户主屏接管"):
            await bridge.perform_action(None, {"action": "tap", "x": 20, "y": 20})
        assert len(peer.requests) == 2
        _, resumed = await bridge.perform_action(None, {"action": "resume_from_user"})
        assert resumed is not None and resumed.takeover_reason is None
        await bridge.perform_action(None, {"action": "tap", "x": 20, "y": 20})
        assert len(peer.requests) == 4


@pytest.mark.asyncio
async def test_ack_at_timeout_boundary_keeps_live_connection(monkeypatch) -> None:
    """真实 ACK 在 wait 超时返回后投递，事件为真时不能误判为未确认。"""
    monkeypatch.setattr(bridge_module, "ACK_TIMEOUT_S", 0.1)
    async with peer_connection("late_ack") as (bridge, peer):
        original_wait = asyncio.wait
        delivered = []
        node_session = bridge._sessions["fault-peer"]

        async def wait_then_deliver_ack(futures, **kwargs):
            # 只控制桥接等待的调度，不替换 asyncio.wait 的真实超时结果。
            pending = node_session.pending.values()
            is_rpc_wait = any(future in pending for future in futures)
            result = await original_wait(futures, **kwargs)
            if is_rpc_wait and not result[0]:
                assert peer.held_ack is not None
                session, frame = peer.held_ack
                bridge.handle_incoming(session, frame)
                assert session.acks[frame["request_id"]].is_set()
                delivered.append(frame["request_id"])
                peer.release_response.set()
            return result

        monkeypatch.setattr(asyncio, "wait", wait_then_deliver_ack)
        assert await bridge.rpc(None, "action", {"action": "tap"}) == {"source": "current"}
        assert len(delivered) == 1
        assert len(peer.requests) == 1
        assert len(bridge.list_devices()) == 1


@pytest.mark.asyncio
async def test_starlette_send_disconnect_becomes_phone_error() -> None:
    """真实监听器经过 Starlette 将 ASGI 写入错误转换成明确的 RPC 错误。"""
    import uvicorn
    from phone_rpc_pkg.server import PhoneListenerServer, create_phone_app

    bridge = PhoneBridge()
    app = create_phone_app(bridge)
    failed_requests = []

    async def fail_request_send(scope, receive, send):
        async def send_frame(message):
            if message["type"] == "websocket.send":
                frame = json.loads(message["text"])
                if frame.get("type") == "request":
                    failed_requests.append(frame)
                    raise OSError("injected ASGI write failure")
            await send(message)

        await app(scope, receive, send_frame)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    server = PhoneListenerServer(uvicorn.Config(
        fail_request_send, log_level="error", lifespan="off", ws="websockets-sansio"
    ))
    running = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await asyncio.wait_for(server.ready.wait(), 2)
        port = listener.getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}/api/phone/ws") as ws:
            await ws.send(json.dumps({"type": "hello", "supports_snapshot_ref": True, "device_id": "asgi-peer", "supports_ack": True}))
            assert json.loads(await ws.recv())["type"] == "welcome"
            with pytest.raises(PhoneNodeError, match="结果不确定，可能已执行"):
                await bridge.rpc(None, "action", {"action": "tap"})
            # Starlette 已将 application_state 标为断开，下一次调用也应是可识别错误。
            with pytest.raises(PhoneNodeError, match="结果不确定，可能已执行"):
                await bridge.rpc(None, "observe", {})
            assert len(failed_requests) == 1
    finally:
        server.should_exit = True
        await asyncio.wait_for(running, 3)
        listener.close()
        await bridge.close_all()


@pytest.mark.asyncio
async def test_snapshot_ref_survives_transport_decode_and_render_without_action_autofill() -> None:
    """同一短编号的新旧快照在真实 WebSocket 上保留不同引用。"""
    screen = {"nodes": [{"node_id": 1, "text": "old", "bounds": [0, 0, 20, 20]}]}
    async with peer_connection("screen", screen) as (bridge, peer):
        first = await bridge.observe(None)
        screen["nodes"][0]["text"] = "new"
        second = await bridge.observe(None)
        assert first.snapshot_ref == peer.requests[0]["request_id"]
        assert second.snapshot_ref == peer.requests[1]["request_id"]
        assert first.snapshot_ref != second.snapshot_ref
        assert first.nodes[0].node_id == second.nodes[0].node_id == 1
        assert f"snapshot_ref={second.snapshot_ref}" in format_screen_snapshot(second)
        _, after = await bridge.perform_action(None, {
            "action": "tap", "node_id": 1, "snapshot_ref": first.snapshot_ref,
        })
        # Bridge preserves the caller's reference; only Android owns acceptance of stale nodes.
        assert peer.requests[-1]["payload"]["snapshot_ref"] == first.snapshot_ref
        assert after is not None and after.snapshot_ref == peer.requests[-1]["request_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["observe", "action"])
async def test_missing_snapshot_ref_is_unknown_after_one_sent_request(op: str) -> None:
    """RPC 成功后缺失快照引用不代表未执行，也不触发自动重发。"""
    async with peer_connection("screen_without_ref", {"nodes": []}) as (bridge, peer):
        with pytest.raises(PhoneNodeError, match="snapshot_ref.*可能已执行.*0.5.0") as caught:
            if op == "observe":
                await bridge.observe(None)
            else:
                await bridge.perform_action(None, {"action": "tap", "x": 10, "y": 10})
        assert caught.value.outcome == "unknown"
        assert len(peer.requests) == 1 and peer.requests[0]["op"] == op
