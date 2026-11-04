"""Phone 端侧反向连接桥与 RPC 会话管理。"""
from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import cast

from .contract import (
    PhoneDeviceDescriptor,
    PhoneDisplayMode,
    PhonePipState,
    PhoneScreenSnapshot,
)
from .safety import detect_takeover_reason, parse_ui_nodes


class PhoneNodeError(RuntimeError):
    """端侧节点不在线、断连、超时或拒绝执行。"""


@dataclass
class _NodeSession:
    device_id: str
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
    pending: dict[str, asyncio.Future[dict[str, object]]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
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
        self._seq = 0

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
        )
        self._sessions[device_id] = session
        return session

    def handle_incoming(self, session: _NodeSession, frame: Mapping[str, object]) -> None:
        """处理端侧发来的状态变更或 RPC 响应帧。"""
        if session.closed:
            return
        frame_type = frame.get("type")
        if frame_type == "state":
            self._apply_state_update(session, frame)
            return
        if frame_type == "response":
            request_id = frame.get("request_id")
            if isinstance(request_id, str):
                fut = session.pending.pop(request_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(dict(frame))
            if isinstance(frame.get("state"), Mapping):
                self._apply_state_update(session, cast(Mapping[str, object], frame["state"]))

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

    async def _evict_session(self, session: _NodeSession, reason: str) -> None:
        if session.closed:
            return
        session.closed = True
        if self._sessions.get(session.device_id) is session:
            del self._sessions[session.device_id]
        for fut in tuple(session.pending.values()):
            if not fut.done():
                fut.set_exception(PhoneNodeError(f"设备 {session.device_id} 连接中断: {reason}"))
        session.pending.clear()
        with suppress(Exception):
            await session.close_transport()

    async def close_all(self) -> None:
        for session in tuple(self._sessions.values()):
            await self._evict_session(session, "Phone 插件正在关闭")

    async def rpc(
        self,
        device_id: str | None,
        op: str,
        payload: Mapping[str, object],
        *,
        timeout_ms: int = 30_000,
    ) -> dict[str, object]:
        target_id = self.resolve_device_id(device_id)
        session = self._sessions.get(target_id)
        if session is None or session.closed:
            raise PhoneNodeError(f"设备 {target_id!r} 不在线")
        async with session.lock:
            if session.closed:
                raise PhoneNodeError(f"设备 {target_id!r} 已断开连接")
            self._seq += 1
            request_id = f"req-{self._seq}"
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[dict[str, object]] = loop.create_future()
            session.pending[request_id] = fut
            try:
                await session.send_json(
                    {
                        "type": "request",
                        "request_id": request_id,
                        "op": op,
                        "payload": dict(payload),
                    }
                )
                response = await asyncio.wait_for(fut, timeout=max(0.1, timeout_ms / 1000.0))
            except TimeoutError as exc:
                raise PhoneNodeError(f"设备 {target_id} 执行 {op} 超时 ({timeout_ms}ms)") from exc
            except PhoneNodeError:
                raise
            except Exception as exc:
                raise PhoneNodeError(f"设备 {target_id} 通信失败: {exc}") from exc
            finally:
                session.pending.pop(request_id, None)
        if not response.get("ok", False):
            error_msg = str(response.get("error") or f"设备 {target_id} 拒绝执行 {op}")
            raise PhoneNodeError(error_msg)
        result = response.get("result")
        return dict(cast(Mapping[str, object], result)) if isinstance(result, Mapping) else {}

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
        # 若当前设备处于待用户主屏接管状态，除 resume_from_user / hand_to_user 外拒绝盲目继续点击
        if session.takeover_reason and action not in ("resume_from_user", "hand_to_user"):
            raise PhoneNodeError(
                f"设备 {target_id} 当前正在等待用户主屏接管 ({session.takeover_reason})；"
                "请先提示用户在手机上完成操作，再调用 resume_from_user 或 phone_observe。"
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
        explicit_takeover = (
            str(raw["takeover_reason"])
            if isinstance(raw.get("takeover_reason"), str) and raw.get("takeover_reason")
            else None
        )
        secure_window = bool(raw.get("secure_window", False))
        takeover = detect_takeover_reason(
            package_name=package_name,
            activity_name=activity_name,
            nodes=nodes,
            explicit_reason=explicit_takeover,
            secure_window=secure_window,
        )
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
        )
