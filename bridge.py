"""Phone 端侧反向连接桥与 RPC 会话管理。"""
from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import cast
from uuid import uuid4

from .contract import (
    PhoneDeviceDescriptor,
    PhoneDisplayMode,
    PhonePipState,
    PhoneScreenSnapshot,
)
from .safety import parse_ui_nodes

logger = logging.getLogger(__name__)
# 端侧收到请求后立即回 ack；超过这个时间没有 ack 视为下行长连接已失效。
ACK_TIMEOUT_S = 6.0
# 插件关闭时只在此宽限内等待已下线连接；取消不等于物理关闭已确认。
CLOSE_TIMEOUT_S = 5.0


class PhoneNodeError(RuntimeError):
    """端侧节点不在线、断连、超时或拒绝执行。"""


@dataclass
class _NodeSession:
    device_id: str
    connection_id: str
    device_name: str
    model: str
    sdk_int: int
    shell_version: str
    display_mode: PhoneDisplayMode
    virtual_display_id: int | None
    own_focus: bool
    steal_top_focus_disabled: bool
    pip_state: PhonePipState
    active_packages: tuple[str, ...]
    takeover_reason: str | None
    send_json: Callable[[dict[str, object]], Awaitable[None]]
    close_transport: Callable[[], Awaitable[None]]
    supports_ack: bool = False
    connected_at: float = 0.0
    pending: dict[str, asyncio.Future[dict[str, object]]] = field(default_factory=dict)
    acks: dict[str, asyncio.Event] = field(default_factory=dict)
    closed: bool = False

    def descriptor(self) -> PhoneDeviceDescriptor:
        return PhoneDeviceDescriptor(
            device_id=self.device_id,
            device_name=self.device_name,
            model=self.model,
            sdk_int=self.sdk_int,
            shell_version=self.shell_version,
            display_mode=self.display_mode,
            virtual_display_id=self.virtual_display_id,
            own_focus=self.own_focus,
            steal_top_focus_disabled=self.steal_top_focus_disabled,
            pip_state=self.pip_state,
            active_packages=self.active_packages,
            takeover_reason=self.takeover_reason,
        )


def _valid_display_mode(value: object) -> PhoneDisplayMode:
    if value in ("virtual_display", "accessibility_only", "pairing_required"):
        return cast(PhoneDisplayMode, value)
    return "virtual_display"


def _valid_pip_state(value: object) -> PhonePipState:
    if value in ("hidden", "running", "needs_you", "done"):
        return cast(PhonePipState, value)
    return "hidden"


class PhoneBridge:
    """维护在线 Android Shell 节点会话，按设备串行分发端侧指令。"""

    def __init__(self) -> None:
        self._sessions: dict[str, _NodeSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._closing_transports: set[asyncio.Task[None]] = set()

    def list_devices(self) -> tuple[PhoneDeviceDescriptor, ...]:
        return tuple(
            session.descriptor()
            for _, session in sorted(self._sessions.items(), key=lambda item: item[0])
            if not session.closed
        )

    def resolve_device_id(self, requested: str | None = None) -> str:
        live = [device_id for device_id, s in sorted(self._sessions.items()) if not s.closed]
        if requested:
            if requested not in self._sessions or self._sessions[requested].closed:
                raise PhoneNodeError(
                    f"设备 {requested!r} 当前不在线（当前在线设备: {', '.join(live) or '无'}）"
                )
            return requested
        if not live:
            raise PhoneNodeError(
                "当前没有在线的 Android 手机端侧设备。请确认手机上 Akashic Shell 已连接并开启后台手机控制。"
            )
        if len(live) > 1:
            raise PhoneNodeError(
                f"当前有 {len(live)} 台手机同时在线 ({', '.join(live)})，请显式传入 device_id（可先调用 phone_devices 查看）。"
            )
        return live[0]

    async def register_node(
        self,
        hello: Mapping[str, object],
        *,
        send_json: Callable[[dict[str, object]], Awaitable[None]],
        close_transport: Callable[[], Awaitable[None]],
    ) -> _NodeSession:
        device_id = str(hello.get("device_id") or "").strip()
        if not device_id or len(device_id) > 128:
            raise ValueError("端侧注册缺少合法的 device_id")
        previous = self._sessions.get(device_id)
        if previous is not None and not previous.closed:
            await self._evict_session(previous, "已被同一 device_id 的新连接接替")
        raw_packages = hello.get("active_packages")
        packages = (
            tuple(str(item) for item in raw_packages if isinstance(item, str) and item)
            if isinstance(raw_packages, list)
            else ()
        )
        raw_vd = hello.get("virtual_display_id")
        vd_id = int(raw_vd) if isinstance(raw_vd, int) and not isinstance(raw_vd, bool) and raw_vd >= 0 else None
        session = _NodeSession(
            device_id=device_id,
            connection_id=f"conn-{uuid4().hex}",
            device_name=str(hello.get("device_name") or device_id),
            model=str(hello.get("model") or "Android"),
            sdk_int=int(hello.get("sdk_int") if isinstance(hello.get("sdk_int"), int) else 34),
            shell_version=str(hello.get("shell_version") or "0.2.1"),
            display_mode=_valid_display_mode(hello.get("display_mode")),
            virtual_display_id=vd_id,
            own_focus=bool(hello.get("own_focus", True)),
            steal_top_focus_disabled=bool(hello.get("steal_top_focus_disabled", True)),
            pip_state=_valid_pip_state(hello.get("pip_state")),
            active_packages=packages,
            takeover_reason=str(hello["takeover_reason"]) if isinstance(hello.get("takeover_reason"), str) and hello.get("takeover_reason") else None,
            send_json=send_json,
            close_transport=close_transport,
            supports_ack=hello.get("supports_ack") is True,
            connected_at=asyncio.get_running_loop().time(),
        )
        self._sessions[device_id] = session
        logger.info(
            "phone.node.registered device=%s shell=%s ack=%s vd=%s",
            device_id, session.shell_version, session.supports_ack, vd_id,
        )
        return session

    def handle_incoming(self, session: _NodeSession, frame: Mapping[str, object]) -> None:
        """处理端侧发来的状态变更或 RPC 响应帧。"""
        if session.closed:
            return
        frame_type = frame.get("type")
        if frame_type == "state":
            self._apply_state_update(session, frame)
            return
        if frame_type == "ack":
            self._handle_ack(session, frame)
            return
        if frame_type == "response":
            request_id = frame.get("request_id")
            if isinstance(request_id, str):
                fut = session.pending.pop(request_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(dict(frame))
                    # 旧请求的迟到回包不能覆盖重连后或后续请求的设备状态。
                    if isinstance(frame.get("state"), Mapping):
                        self._apply_state_update(session, cast(Mapping[str, object], frame["state"]))

    # 端侧确认收到请求；顺带上报上次回传失败等端侧诊断。
    def _handle_ack(self, session: _NodeSession, frame: Mapping[str, object]) -> None:
        request_id = frame.get("request_id")
        if isinstance(request_id, str) and (event := session.acks.get(request_id)) is not None:
            event.set()
        errors = frame.get("recent_errors")
        if isinstance(errors, list) and errors:
            logger.warning(
                "phone.node.diagnostics device=%s errors=%s",
                session.device_id, [str(item)[:300] for item in errors[:10]],
            )

    def handle_incoming_for_device(self, device_id: str, frame: Mapping[str, object]) -> bool:
        """只接收当前连接所属的 HTTP 回传，拒绝旧线程迟到的状态。"""
        session = self._sessions.get(device_id)
        if session is None or session.closed:
            return False
        if frame.get("connection_id") != session.connection_id:
            raise PhoneNodeError("回传所属连接已过期，请使用当前 welcome 的 connection_id")
        self.handle_incoming(session, frame)
        return True

    def _apply_state_update(self, session: _NodeSession, data: Mapping[str, object]) -> None:
        if "display_mode" in data:
            session.display_mode = _valid_display_mode(data.get("display_mode"))
        if "virtual_display_id" in data:
            raw_vd = data.get("virtual_display_id")
            session.virtual_display_id = (
                int(raw_vd) if isinstance(raw_vd, int) and not isinstance(raw_vd, bool) and raw_vd >= 0 else None
            )
        if "own_focus" in data:
            session.own_focus = bool(data.get("own_focus"))
        if "steal_top_focus_disabled" in data:
            session.steal_top_focus_disabled = bool(data.get("steal_top_focus_disabled"))
        if "pip_state" in data:
            session.pip_state = _valid_pip_state(data.get("pip_state"))
        if "active_packages" in data and isinstance(data["active_packages"], list):
            session.active_packages = tuple(
                str(item) for item in data["active_packages"] if isinstance(item, str) and item
            )
        if "takeover_reason" in data:
            reason = data.get("takeover_reason")
            session.takeover_reason = str(reason) if isinstance(reason, str) and reason else None

    async def unregister_node(self, session: _NodeSession, reason: str = "端侧连接已断开") -> None:
        await self._evict_session(session, reason)

    async def _evict_session(self, session: _NodeSession, reason: str, *, deadline: float | None = None) -> None:
        """先逻辑下线，再跟踪传输关闭；不以网络背压延长调用期限。"""
        if session.closed:
            return
        # 1. 立即撤销路由并结算在途请求，不等待物理关闭。
        session.closed = True
        if self._sessions.get(session.device_id) is session:
            del self._sessions[session.device_id]
        age = asyncio.get_running_loop().time() - session.connected_at
        logger.info("phone.node.closed device=%s reason=%s age_s=%.0f", session.device_id, reason, age)
        for fut in tuple(session.pending.values()):
            if not fut.done():
                fut.set_exception(PhoneNodeError(f"设备 {session.device_id} 连接中断: {reason}"))
        session.pending.clear()
        # 2. 关闭任务由桥接持有，即使原 RPC 已超时也不丢失 owner。
        closing = asyncio.create_task(session.close_transport(), name=f"phone-close:{session.device_id}")
        self._closing_transports.add(closing)
        closing.add_done_callback(self._transport_closed)
        if deadline is None:
            await asyncio.shield(closing)
        elif (remaining := deadline - asyncio.get_running_loop().time()) > 0:
            await asyncio.wait((closing,), timeout=remaining)

    def _transport_closed(self, task: asyncio.Task[None]) -> None:
        self._closing_transports.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("phone.transport.close_failed task=%s", task.get_name(),
                         exc_info=(type(error), error, error.__traceback__))

    async def close_all(self) -> None:
        """下线所有节点，有界等待关闭；未确认关闭必须保留日志。"""
        for session in tuple(self._sessions.values()):
            await self._evict_session(session, "Phone 插件正在关闭", deadline=asyncio.get_running_loop().time())
        if self._closing_transports:
            _, pending = await asyncio.wait(tuple(self._closing_transports), timeout=CLOSE_TIMEOUT_S)
            for task in pending:
                logger.warning("phone.transport.close_unconfirmed task=%s grace_s=%.1f", task.get_name(), CLOSE_TIMEOUT_S)
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def rpc(
        self,
        device_id: str | None,
        op: str,
        payload: Mapping[str, object],
        *,
        timeout_ms: int = 30_000,
    ) -> dict[str, object]:
        """排队等待受调用期限约束，过期请求不再发往端侧。"""
        target_id = self.resolve_device_id(device_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.1, timeout_ms / 1000.0)
        lock = self._locks.setdefault(target_id, asyncio.Lock())
        not_sent = f"设备 {target_id} 的 {op} 请求排队超时 ({timeout_ms}ms)，未发送到端侧。"
        # 1. 排队也消耗调用期限，超时不意味着端侧出现未知效果。
        try:
            async with asyncio.timeout_at(deadline):
                await lock.acquire()
        except TimeoutError as exc:
            raise PhoneNodeError(not_sent) from exc
        try:
            # 锁可立即取得时未必让出事件循环，仍须拒绝已经过期的请求。
            if loop.time() >= deadline:
                raise PhoneNodeError(not_sent)
            # 2. 发送后的断连或超时由 _send_request 报告结果不确定。
            # ACK 或结果丢失不能证明端侧未执行；每次调用只发送一次。
            response = await self._send_request(self._live_session(target_id), op, payload, deadline, timeout_ms)
        finally:
            lock.release()
        if not response.get("ok", False):
            error_msg = str(response.get("error") or f"设备 {target_id} 拒绝执行 {op}")
            raise PhoneNodeError(error_msg)
        result = response.get("result")
        return dict(cast(Mapping[str, object], result)) if isinstance(result, Mapping) else {}

    def _live_session(self, device_id: str) -> _NodeSession:
        session = self._sessions.get(device_id)
        if session is None or session.closed:
            raise PhoneNodeError(f"设备 {device_id!r} 不在线")
        return session

    async def _send_request(
        self,
        session: _NodeSession,
        op: str,
        payload: Mapping[str, object],
        deadline: float,
        timeout_ms: int,
    ) -> dict[str, object]:
        """发送一次请求；无法取得结果时保留端侧可能已经执行的事实。"""
        loop = asyncio.get_running_loop()
        # 端侧去重表可跨连接保留，不能因服务端重启复用 req-1 等 ID。
        request_id = f"req-{uuid4().hex}"
        fut: asyncio.Future[dict[str, object]] = loop.create_future()
        ack = asyncio.Event()
        session.pending[request_id] = fut
        session.acks[request_id] = ack
        started = loop.time()
        tag = f"device={session.device_id} op={op} request={request_id}"
        try:
            # 1. 传输写入也消耗原有期限；取消写入不能证明端侧没有收到。
            try:
                async with asyncio.timeout_at(deadline):
                    await session.send_json({"type": "request", "request_id": request_id, "op": op, "payload": dict(payload)})
            except TimeoutError as exc:
                raise PhoneNodeError(f"发送请求超时 ({timeout_ms}ms)") from exc
            # 2. 等端侧确认收到；回包先到也算确认
            if session.supports_ack and not await _wait_ack(ack, fut, min(ACK_TIMEOUT_S, deadline - loop.time())):
                logger.warning("phone.rpc.unacked %s waited_ms=%d", tag, (loop.time() - started) * 1000)
                _ = session.pending.pop(request_id, None)
                fut.cancel()
                await self._evict_session(session, "请求未被端侧确认，下行连接疑似已失效", deadline=deadline)
                raise PhoneNodeError("未收到端侧确认，下行连接疑似失效")
            acked_ms = (loop.time() - started) * 1000
            # 3. 等回包，不给后续阶段追加最小等待窗口。
            try:
                response = await asyncio.wait_for(fut, timeout=max(0.0, deadline - loop.time()))
            except TimeoutError as exc:
                logger.warning("phone.rpc.timeout %s acked=%s ack_ms=%d", tag, ack.is_set(), acked_ms)
                stage = "已收到请求但未在时限内回传结果" if ack.is_set() else "无响应"
                raise PhoneNodeError(f"设备 {session.device_id} 执行 {op} 超时 ({timeout_ms}ms)：{stage}") from exc
        except (PhoneNodeError, OSError) as exc:
            raise PhoneNodeError(
                f"设备 {session.device_id} 执行 {op} 的结果不确定，可能已执行；"
                f"未自动重试。请在设备在线后先调用 phone_observe 核实，再决定是否继续。原因：{exc}"
            ) from exc
        finally:
            _ = session.pending.pop(request_id, None)
            _ = session.acks.pop(request_id, None)
        logger.info(
            "phone.rpc.done %s ack_ms=%d total_ms=%d timing=%s",
            tag, acked_ms, (loop.time() - started) * 1000, response.get("timing"),
        )
        return response

    async def observe(
        self,
        device_id: str | None,
        *,
        include_screenshot: bool = False,
        timeout_ms: int = 20_000,
    ) -> PhoneScreenSnapshot:
        target_id = self.resolve_device_id(device_id)
        raw = await self.rpc(
            target_id,
            "observe",
            {"include_screenshot": include_screenshot},
            timeout_ms=timeout_ms,
        )
        return self._decode_snapshot(target_id, raw)

    async def perform_action(
        self,
        device_id: str | None,
        payload: Mapping[str, object],
        *,
        timeout_ms: int = 30_000,
    ) -> tuple[str, PhoneScreenSnapshot | None]:
        target_id = self.resolve_device_id(device_id)
        session = self._sessions.get(target_id)
        if session is None or session.closed:
            raise PhoneNodeError(f"设备 {target_id!r} 不在线")
        action = str(payload.get("action") or "")
        # 只尊重明确的接管状态，不根据页面内容推断用户意图。
        if session.takeover_reason and action not in ("resume_from_user", "hand_to_user"):
            raise PhoneNodeError(
                f"设备 {target_id} 当前正在等待用户主屏接管 ({session.takeover_reason})；"
                "可先调用 phone_observe 查看状态；用户完成后调用 resume_from_user。"
            )
        raw = await self.rpc(target_id, "action", payload, timeout_ms=timeout_ms)
        summary = str(raw.get("message") or f"已执行 {action}")
        raw_screen = raw.get("screen")
        snapshot = (
            self._decode_snapshot(target_id, cast(Mapping[str, object], raw_screen))
            if isinstance(raw_screen, Mapping)
            else None
        )
        return summary, snapshot

    async def finish_display(
        self,
        device_id: str | None,
        *,
        keep_packages: tuple[str, ...] = (),
        close_packages: tuple[str, ...] = (),
        reason: str = "completed",
        timeout_ms: int = 15_000,
    ) -> dict[str, object]:
        target_id = self.resolve_device_id(device_id)
        return await self.rpc(
            target_id,
            "finish",
            {
                "keep": list(keep_packages),
                "close": list(close_packages),
                "reason": reason,
            },
            timeout_ms=timeout_ms,
        )

    def _decode_snapshot(self, device_id: str, raw: Mapping[str, object]) -> PhoneScreenSnapshot:
        session = self._sessions.get(device_id)
        nodes = parse_ui_nodes(raw.get("nodes"))
        package_name = str(raw.get("package_name") or "com.android.shell")
        activity_name = str(raw.get("activity_name") or "MainActivity")
        # 缺少字段不表示用户已结束接管；清除状态必须由端侧明确上报。
        takeover = session.takeover_reason if session is not None else None
        if "takeover_reason" in raw:
            reason = raw["takeover_reason"]
            takeover = reason if isinstance(reason, str) and reason else None
        secure_window = bool(raw.get("secure_window", False))
        pip_state = _valid_pip_state(raw.get("pip_state", "needs_you" if takeover else "running"))
        if takeover:
            pip_state = "needs_you"
        if session is not None and not session.closed:
            session.takeover_reason = takeover
            session.pip_state = pip_state
            raw_disp = raw.get("display_id")
            if isinstance(raw_disp, int) and not isinstance(raw_disp, bool) and raw_disp >= 0:
                session.virtual_display_id = raw_disp
        png_bytes: bytes | None = None
        raw_image = raw.get("screenshot_b64")
        if isinstance(raw_image, str) and raw_image:
            png_bytes = base64.b64decode(raw_image, validate=True)
        return PhoneScreenSnapshot(
            device_id=device_id,
            display_id=int(
                raw.get("display_id")
                if isinstance(raw.get("display_id"), int)
                else (session.virtual_display_id or 2)
                if session
                else 2
            ),
            width=int(raw.get("width") if isinstance(raw.get("width"), int) else 1080),
            height=int(raw.get("height") if isinstance(raw.get("height"), int) else 2400),
            rotation=int(raw.get("rotation") if isinstance(raw.get("rotation"), int) else 0),
            package_name=package_name,
            activity_name=activity_name,
            pip_state=pip_state,
            audio_muted=bool(raw.get("audio_muted", True)),
            nodes=nodes,
            screenshot_png=png_bytes,
            takeover_reason=takeover,
            secure_window=secure_window,
        )


# 等 ack 或回包任一到达；超时返回 False。
async def _wait_ack(ack: asyncio.Event, fut: asyncio.Future[dict[str, object]], timeout_s: float) -> bool:
    if ack.is_set() or fut.done():
        return True
    waiter = asyncio.ensure_future(ack.wait())
    try:
        done, _ = await asyncio.wait((waiter, fut), timeout=max(0.0, timeout_s), return_when=asyncio.FIRST_COMPLETED)
    finally:
        if not waiter.done():
            _ = waiter.cancel()
    # 超时回调和接收回调可能同轮运行，wait 的 done 集合不一定反映最新状态。
    return bool(done) or ack.is_set() or fut.done()
