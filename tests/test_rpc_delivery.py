"""通过真实本地 WebSocket 注入丢包和断连，不代表 Android 真机验收。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
from pathlib import Path
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

PhoneBridge = bridge_module.PhoneBridge
PhoneNodeError = bridge_module.PhoneNodeError


class ProtocolPeer:
    """记录实际收到的请求，并按指定故障丢弃确认或结果。"""

    def __init__(self, bridge, fault: str) -> None:
        self.bridge = bridge
        self.fault = fault
        self.requests: list[dict] = []
        self.connections = asyncio.Queue()
        self.old_id: str | None = None
        self.stale_sent = asyncio.Event()
        self.release_response = asyncio.Event()

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
                self.bridge.handle_incoming(session, json.loads(raw))
        finally:
            await self.bridge.unregister_node(session)

    async def run(self, url: str) -> None:
        """断连后重新注册，保留请求记录以发现自动重放。"""
        while True:
            async with connect(url) as ws:
                await ws.send(json.dumps({
                    "type": "hello", "device_id": "fault-peer", "supports_ack": True
                }))
                await ws.recv()
                await self.connections.put(ws)
                async for raw in ws:
                    frame = json.loads(raw)
                    self.requests.append(frame)
                    request_id = frame["request_id"]
                    if len(self.requests) == 1:
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
async def peer_connection(fault: str):
    """为每个场景使用独立端口和会话，退出时完整关闭。"""
    bridge = PhoneBridge()
    peer = ProtocolPeer(bridge, fault)
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
