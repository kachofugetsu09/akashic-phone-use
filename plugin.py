"""Phone 端侧能力外置插件（phone-use）：按需加载（load_tools）、无线调试后台虚拟屏控制与 Turn 级生命周期。"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import ValidationError

from agent.plugin_composition import RUNTIME_STARTED, Context, ServiceKey
from core.common.file_io import run_file_io
from plugins.assets.contract import INSTALLED_ASSETS
from plugins.ledger.contract import (
    ARTIFACT_IMPORT,
    BINDINGS,
    MESSAGE_CATALOG,
    OWNER_STATE,
    ContentPart,
    Control,
    Input,
    Message,
    MessageCatalog,
    Output,
    OwnerRecord,
    OwnerStore,
    json_value,
)
from plugins.tools.contract import TOOLS, CallSource, Result
from plugins.turn_projection.contract import TURN_PROJECTION, TurnProjection

from .bridge import PhoneBridge, PhoneNodeError
from .contract import PHONE_BRIDGE, PhoneScreenSnapshot
from .server import start_phone_listener
from .tools import (
    PhoneActionInput,
    PhoneDevicesInput,
    PhoneDevicesTool,
    PhoneObserveInput,
    extract_turn_identity,
    parse_target,
    resolve_app,
    snapshot_to_parts,
)

PHONE_CONTROL = ServiceKey["PhoneControl"]("phone.control.v1")


@dataclass(frozen=True, slots=True)
class PhoneCallIdentity:
    session_id: str
    source: str
    turn_input_id: str
    device_id: str


class _TurnSuperseded(RuntimeError):
    """设备已经交给另一个 Turn；旧 Turn 不能再释放它。"""


def _parse_call_identity(arguments: Mapping[str, object], default_device: str) -> PhoneCallIdentity:
    value = arguments.get("_phone")
    if not isinstance(value, Mapping) or set(value) != {"session_id", "source", "turn_input_id"}:
        raise ValueError("Phone 工具缺少已验证的 Turn 身份")
    if any(not isinstance(value[k], str) or not value[k] for k in value):
        raise ValueError("Phone Turn 身份字段无效")
    return PhoneCallIdentity(
        session_id=cast(str, value["session_id"]),
        source=cast(str, value["source"]),
        turn_input_id=cast(str, value["turn_input_id"]),
        device_id=default_device,
    )


def _same_record(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    fields = ("v", "control_binding", "session_id", "source", "turn_input_id", "device_id")
    return all(left.get(field) == right.get(field) for field in fields)


_STEP_FIELDS = (
    "action", "package_name", "node_id", "x", "y", "end_x", "end_y", "duration_ms",
    "text", "direction", "key_code", "uri", "reason",
)
_WAIT_FOR_SECONDS = 8.0
_WAIT_FOR_POLL_SECONDS = 0.6


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    summary: str
    snapshot: PhoneScreenSnapshot | None
    error: str | None = None


# 单步参数转端侧线格式：target 解析为 node_id 或 selector；只有最后一步按需附截图。
def wire_payload(step: Mapping[str, object], *, last: bool, include_screenshot: bool) -> dict[str, object]:
    payload: dict[str, object] = {k: step[k] for k in _STEP_FIELDS if step.get(k) is not None}
    target = step.get("target")
    if isinstance(target, str):
        payload.update(parse_target(target))
    payload["auto_screenshot"] = last
    payload["include_screenshot"] = last and include_screenshot
    return payload


def _has_text(snapshot: PhoneScreenSnapshot | None, wanted: str) -> bool:
    folded = wanted.casefold()
    return snapshot is not None and any(
        folded in node.name.casefold() or folded in (node.value or "").casefold() for node in snapshot.nodes
    )


# 首步就失败时保留原结果确定性；已有步骤执行过则带回最后确认的界面。
def _stopped(
    done: list[str], snapshot: PhoneScreenSnapshot | None, label: str, error: Exception,
) -> ActionOutcome:
    message = f"{label}失败：{error}"
    if isinstance(error, PhoneNodeError) and error.outcome == "unknown":
        message += "（结果不确定，可能已执行，先观察再决定）"
    if not done:
        outcome: Literal["not_sent", "unknown"] = (
            error.outcome if isinstance(error, PhoneNodeError) else "not_sent"
        )
        raise PhoneNodeError(message if label != "动作" else str(error), outcome=outcome) from error
    return ActionOutcome("\n".join(done), snapshot, f"{message}；之前的步骤已执行，下方界面是上一步确认后的状态")


class PhoneControl:
    """拥有 Phone effect，并在源 Turn 闭合后自动通知端侧收尾后台虚拟屏。"""

    def __init__(self, ctx: Context, bridge: PhoneBridge) -> None:
        self.ctx = ctx
        self.bridge = bridge

    def _state(self) -> OwnerStore:
        return self.ctx.require(OWNER_STATE).open(self.ctx)

    async def _record_started(
        self,
        key: str,
        control_binding: str,
        identity: PhoneCallIdentity,
    ) -> None:
        """在设备发送锁内原子保存调用身份与最新 Turn 使用权。"""
        owner_key = "phone-use:" + key
        value: dict[str, object] = {
            "v": 1,
            "phase": "started",
            "control_binding": control_binding,
            "session_id": identity.session_id,
            "source": identity.source,
            "turn_input_id": identity.turn_input_id,
            "device_id": identity.device_id,
        }
        state = self._state()

        def claim(tx) -> None:
            existing = tx.read(owner_key)
            if existing is None:
                _ = tx.save(owner_key, value, expected_version=None)
            elif not _same_record(existing.value, value):
                raise ValueError("Phone 调用回执身份不一致")
            elif existing.value.get("phase") != "started":
                raise RuntimeError("Phone 调用已经结束或收尾结果未知")
            # 此回执仅表示当前设备使用权；不改写任何既有调用的身份或结果。
            device_key = "phone-device:" + identity.device_id
            current = tx.read(device_key)
            _ = tx.save(device_key, {
                "v": 1, "control_binding": control_binding,
                "session_id": identity.session_id, "source": identity.source,
                "turn_input_id": identity.turn_input_id, "device_id": identity.device_id,
            }, expected_version=current.version if current else None)

        await state.transact_async(claim)

    async def observe(
        self,
        key: str,
        control_binding: str,
        arguments: Mapping[str, object],
    ) -> PhoneScreenSnapshot:
        requested_device = cast(str | None, arguments.get("device_id"))
        device_id = self.bridge.resolve_device_id(requested_device)
        identity = _parse_call_identity(arguments, device_id)
        return await self.bridge.observe(
            device_id,
            include_screenshot=bool(arguments.get("include_screenshot", False)),
            auto_screenshot=True,
            timeout_ms=int(arguments.get("timeout_ms", 20_000)),
            before_send=lambda: self._record_started(key, control_binding, identity),
        )

    # 1. 首步认领设备，后续步骤与轮询只核对归属 2. 任一步失败即停并带回最后确认的界面
    # 3. wait_for 超时不否认已执行的动作。
    async def action(
        self,
        key: str,
        control_binding: str,
        arguments: Mapping[str, object],
    ) -> ActionOutcome:
        requested_device = cast(str | None, arguments.get("device_id"))
        device_id = self.bridge.resolve_device_id(requested_device)
        identity = _parse_call_identity(arguments, device_id)
        raw_steps = arguments.get("steps")
        # 参数经调用回执冻结后列表可能变成元组，按序列判断。
        steps = (
            list(cast(Sequence[Mapping[str, object]], raw_steps))
            if isinstance(raw_steps, Sequence) and not isinstance(raw_steps, (str, bytes)) else [arguments]
        )
        timeout_ms = int(cast(int, arguments.get("timeout_ms", 30_000)))
        include_screenshot = bool(arguments.get("include_screenshot", False))
        done: list[str] = []
        snapshot: PhoneScreenSnapshot | None = None
        for index, step in enumerate(steps):
            last = index == len(steps) - 1
            label = f"第 {index + 1}/{len(steps)} 步 {step.get('action')}" if len(steps) > 1 else "动作"
            guard = (lambda: self._record_started(key, control_binding, identity)) if index == 0 \
                else (lambda: self._ensure_owner(identity))
            try:
                payload = wire_payload(await self._resolve_app(device_id, step), last=last,
                                       include_screenshot=include_screenshot)
                self._check_shell_support(device_id, payload)
                summary, snapshot = await self.bridge.perform_action(
                    device_id, payload, timeout_ms=timeout_ms, before_send=guard,
                )
            except (PhoneNodeError, ValueError, _TurnSuperseded) as error:
                return _stopped(done, snapshot, label, error)
            wanted = step.get("wait_for")
            if isinstance(wanted, str):
                waited, snapshot, found = await self._wait_for(
                    device_id, identity, wanted, snapshot, last and include_screenshot,
                )
                summary = f"{summary}；{waited}"
                if not found:
                    done.append(f"{label}：{summary}" if len(steps) > 1 else summary)
                    return ActionOutcome("\n".join(done), snapshot, f"{label}已执行，但{waited}")
            done.append(f"{label}：{summary}" if len(steps) > 1 else summary)
        return ActionOutcome("\n".join(done), snapshot)

    def _check_shell_support(self, device_id: str, payload: Mapping[str, object]) -> None:
        if self.bridge.supports_tree_v2(device_id):
            return
        if "node_id" in payload or "selector" in payload or payload.get("action") in ("type_text", "clear_text"):
            raise PhoneNodeError("当前 Akashic Shell 版本过旧，不支持新的元素定位；请升级 Shell 至 0.5.2")

    async def _resolve_app(self, device_id: str, step: Mapping[str, object]) -> Mapping[str, object]:
        app = step.get("app")
        if not isinstance(app, str):
            return step
        if not self.bridge.supports_apps(device_id):
            if "." in app and " " not in app.strip():
                return {**step, "package_name": app.strip()}
            raise ValueError("当前 Akashic Shell 不支持按应用名打开，请提供包名或升级 Shell 至 0.5.2")
        try:
            package = resolve_app(await self.bridge.launcher_apps(device_id), app)
        except ValueError:
            # 连接期间新装的应用不在缓存里，找不到时刷新一次清单。
            package = resolve_app(await self.bridge.launcher_apps(device_id, refresh=True), app)
        return {**step, "package_name": package}

    async def _wait_for(
        self,
        device_id: str,
        identity: PhoneCallIdentity,
        wanted: str,
        snapshot: PhoneScreenSnapshot | None,
        include_screenshot: bool,
    ) -> tuple[str, PhoneScreenSnapshot | None, bool]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _WAIT_FOR_SECONDS
        while not _has_text(snapshot, wanted):
            await asyncio.sleep(min(_WAIT_FOR_POLL_SECONDS, max(0.0, deadline - loop.time())))
            remaining_ms = int((deadline - loop.time()) * 1000)
            if remaining_ms <= 0:
                return f"等待 {_WAIT_FOR_SECONDS:.0f} 秒后界面仍没有出现 \"{wanted}\"", snapshot, False
            try:
                snapshot = await self.bridge.observe(
                    device_id, include_screenshot=include_screenshot, auto_screenshot=True,
                    timeout_ms=remaining_ms, before_send=lambda: self._ensure_owner(identity),
                )
            except (PhoneNodeError, _TurnSuperseded) as error:
                return f"等待 \"{wanted}\" 时读屏失败：{error}", snapshot, False
        return f"已出现 \"{wanted}\"", snapshot, True

    # 设备归属核对在发送锁内执行，与新 Turn 的认领串行；被接替就停。
    async def _ensure_owner(self, identity: PhoneCallIdentity) -> None:
        state = self._state()
        device_key = "phone-device:" + identity.device_id
        current = await run_file_io(lambda: state.read(device_key))
        if current is None:
            return
        actual = tuple(current.value.get(field) for field in ("session_id", "source", "turn_input_id"))
        if actual != (identity.session_id, identity.source, identity.turn_input_id):
            raise _TurnSuperseded("设备已由新的对话使用，停止本次操作")

    # 仅当前 Turn 可收尾：设备已被新 Turn 占用时不发送 finish，避免释放它正在用的屏幕。
    async def end_turn(self, identity: PhoneCallIdentity) -> None:
        try:
            _ = await self.bridge.finish_display(
                identity.device_id, reason="turn_ended", before_send=lambda: self._ensure_owner(identity),
            )
        except (_TurnSuperseded, PhoneNodeError):
            # 被接替或手机离线时不重试收尾，下一次使用会复用同一块后台显示器。
            pass


class _PhoneObserveTool:
    idempotent = False

    def __init__(self, control: PhoneControl, control_binding: str, projection: TurnProjection) -> None:
        self._control = control
        self._control_binding = control_binding
        self._projection = projection

    async def prepare(
        self, arguments: Mapping[str, object], source: CallSource | None = None
    ) -> Mapping[str, object] | str:
        try:
            parsed = PhoneObserveInput.model_validate(json_value(arguments)).model_dump(mode="json")
        except ValidationError as error:
            return str(error)
        parsed["_phone"] = extract_turn_identity(source, self._projection)
        return parsed

    async def invoke(self, key: str, arguments: Mapping[str, object]) -> Result:
        try:
            snapshot = await self._control.observe(key, self._control_binding, arguments)
        except PhoneNodeError as error:
            return Result("error", (ContentPart("text", str(error)),))
        parts = await snapshot_to_parts(self._control.ctx, snapshot)
        return Result("success", parts)

    async def query(self, key: str) -> Result | None:
        _ = key
        return None


class _PhoneActionTool:
    idempotent = False

    def __init__(self, control: PhoneControl, control_binding: str, projection: TurnProjection) -> None:
        self._control = control
        self._control_binding = control_binding
        self._projection = projection

    async def prepare(
        self, arguments: Mapping[str, object], source: CallSource | None = None
    ) -> Mapping[str, object] | str:
        try:
            parsed = PhoneActionInput.model_validate(json_value(arguments)).model_dump(mode="json")
        except ValidationError as error:
            return str(error)
        parsed["_phone"] = extract_turn_identity(source, self._projection)
        return parsed

    async def invoke(self, key: str, arguments: Mapping[str, object]) -> Result:
        try:
            outcome = await self._control.action(key, self._control_binding, arguments)
        except PhoneNodeError as error:
            return Result("error", (ContentPart("text", str(error)),))
        status = "error" if outcome.error else "success"
        text = f"{outcome.error}\n{outcome.summary}" if outcome.error else outcome.summary
        if outcome.snapshot is None:
            return Result(status, (ContentPart("text", text),))
        parts = await snapshot_to_parts(self._control.ctx, outcome.snapshot, prefix_text=text)
        return Result(status, parts)

    async def query(self, key: str) -> Result | None:
        _ = key
        return None


api_version = 3
name = "phone-use"
version = "0.4.1"
desc = "连接并后台操作在线的 Android 手机端侧设备（无线调试虚拟屏、悬浮小窗预览、读屏与接管）"
author = "Akashic"
inject = (
    INSTALLED_ASSETS,
    TOOLS,
    BINDINGS,
    MESSAGE_CATALOG,
    OWNER_STATE,
    ARTIFACT_IMPORT,
    TURN_PROJECTION,
)


def _capture(ctx: Context, configuration: Mapping[str, object]) -> Mapping[str, object]:
    if configuration:
        raise ValueError("Phone binding 不接受额外配置")
    identity = ctx.require(BINDINGS).bind(
        PHONE_CONTROL,
        {"version": 1},
        contributors=(ctx,),
    )
    return {"control_binding": identity}


@asynccontextmanager
async def _open_observe(ctx: Context, state: Mapping[str, object]) -> AsyncIterator[_PhoneObserveTool]:
    if set(state) != {"control_binding"} or not isinstance(state["control_binding"], str):
        raise ValueError("Phone binding state 无效")
    async with ctx.require(BINDINGS).open(state["control_binding"], PHONE_CONTROL) as (control, _):
        yield _PhoneObserveTool(control, state["control_binding"], ctx.require(TURN_PROJECTION))


@asynccontextmanager
async def _open_action(ctx: Context, state: Mapping[str, object]) -> AsyncIterator[_PhoneActionTool]:
    if set(state) != {"control_binding"} or not isinstance(state["control_binding"], str):
        raise ValueError("Phone binding state 无效")
    async with ctx.require(BINDINGS).open(state["control_binding"], PHONE_CONTROL) as (control, _):
        yield _PhoneActionTool(control, state["control_binding"], ctx.require(TURN_PROJECTION))


async def apply(ctx: Context) -> None:
    """注册按需加载的 Phone 工具组、无线调试 Bridge 监听器与 Turn 收尾跟随器。"""
    await ctx.require(INSTALLED_ASSETS).register(ctx, "skills", "skills")
    wake_reconcile = asyncio.Event()
    pending_reconcile: dict[str, str] = {}

    def on_node_registered(session) -> None:
        if session.virtual_display_id is not None or session.pip_state in ("running", "needs_you"):
            pending_reconcile[session.device_id] = session.connection_id
            wake_reconcile.set()

    bridge = PhoneBridge(on_registered=on_node_registered)
    control = PhoneControl(ctx, bridge)
    _ = await ctx.provide(PHONE_BRIDGE, bridge)
    _ = await ctx.provide(PHONE_CONTROL, control)

    await start_phone_listener(ctx, bridge)

    devices_tool = PhoneDevicesTool(bridge)

    @asynccontextmanager
    async def _open_devices(_state: Mapping[str, object]) -> AsyncIterator[PhoneDevicesTool]:
        yield devices_tool

    catalog = ctx.require(TOOLS)
    _ = await catalog.declare_group(ctx, always_on=False, description=desc)
    await catalog.register(
        ctx,
        name="phone_devices",
        description=(
            "列出当前连接到 Akashic 服务端的 Android 手机端侧设备、后台虚拟屏幕状态（VirtualDisplay / "
            "own_focus / steal_top_focus_disabled）、右下角悬浮小窗状态、是否等待用户主屏幕接管，"
            "以及手机上已装应用的显示名（launch_app 直接用 app=显示名）。在操作手机前先调用本工具确认设备在线。"
        ),
        parameters=PhoneDevicesInput.model_json_schema(),
        open=_open_devices,
        idempotent=True,
        parallel=True,
    )
    await catalog.register(
        ctx,
        name="phone_observe",
        description=(
            "观察手机后台虚拟屏当前界面：紧凑的元素树（一行一个控件，带跨快照不变的 @编号），"
            "界面读不到控件时自动附截图；可能初始化或唤醒虚拟屏。首次调用前先阅读 phone-use 技能。"
        ),
        parameters=PhoneObserveInput.model_json_schema(),
        open=lambda state: _open_observe(ctx, state),
        capture=lambda options: _capture(ctx, options),
        idempotent=False,
    )
    await catalog.register(
        ctx,
        name="phone_action",
        description=(
            "在手机端后台虚拟屏幕（VirtualDisplay）上执行单步操作：启动应用（launch_app）、点击节点或坐标（tap）、"
            "设置输入框全文（type_text，覆盖原有内容，无需先清空；需检查字段内容）、滚动（scroll）、滑动（swipe）、按键（press_key/back）、"
            "打开链接（open_link）、分享文本（share_text）、按用户要求移交手机主屏幕（hand_to_user）"
            "以及用户完成后收回后台虚拟屏幕（resume_from_user）。用 target 指定元素（@编号或可见文字）；"
            "动作会等界面稳定后返回最新快照，不需要再插 wait 或 phone_observe。"
        ),
        parameters=PhoneActionInput.model_json_schema(),
        open=lambda state: _open_action(ctx, state),
        capture=lambda options: _capture(ctx, options),
        idempotent=False,
    )

    async def start_follower(_event: object) -> None:
        _ = await ctx.spawn(
            _start_follower(ctx, bridge, wake_reconcile, pending_reconcile),
            name="phone-turn-follower",
        )

    _ = await ctx.on(RUNTIME_STARTED, start_follower)


def _turn_closed(
    snapshot: tuple[Message, ...],
    source: str,
    turn_input_id: str,
    projection: TurnProjection,
) -> bool:
    """TurnProjection 闭合，或源 Input 之后出现未恢复的 failure/pause/abandon 控制帧即视为已结束。"""
    turns = projection.project(snapshot, source)
    if any(
        turn.status in {"complete", "quiet", "abandoned"}
        and turn_input_id in turn.message_ids
        for turn in turns
    ):
        return True
    input_seq = next(
        (
            message.seq
            for message in snapshot
            if message.source == source and message.message_id == turn_input_id
        ),
        None,
    )
    if input_seq is None:
        return False
    failed = False
    paused = False
    for message in snapshot:
        if message.source != source or message.seq <= input_seq:
            continue
        body = message.body
        if isinstance(body, Control):
            if body.action in {"failure", "abandon"}:
                failed = True
            elif body.action == "pause":
                paused = True
            elif body.action == "resume":
                paused = False
    return failed or paused


async def _start_follower(
    ctx: Context,
    bridge: PhoneBridge,
    wake_reconcile: asyncio.Event,
    pending_reconcile: dict[str, str],
) -> None:
    """扫描 started phone-use 记录与新接入设备，源 Turn 闭合/失败/暂停或无主残留时收尾虚拟屏幕。"""
    catalog = ctx.require(MESSAGE_CATALOG)
    projection = ctx.require(TURN_PROJECTION)
    state: OwnerStore | None = None
    checked: dict[tuple[str, str, str, str, str], int] = {}

    async def once() -> set[str]:
        nonlocal state
        if state is None:
            state = ctx.require(OWNER_STATE).open(ctx)
        assert state is not None
        groups: dict[tuple[str, str, str, str, str], list[tuple[str, OwnerRecord]]] = {}
        for key, record in await run_file_io(state.list):
            value = record.value
            if not key.startswith("phone-use:") or value.get("phase") != "started":
                continue
            group = (
                cast(str, value.get("control_binding")),
                cast(str, value.get("session_id")),
                cast(str, value.get("source")),
                cast(str, value.get("turn_input_id")),
                cast(str, value.get("device_id")),
            )
            groups.setdefault(group, []).append((key, record))
        for group in tuple(checked):
            if group not in groups:
                del checked[group]
        active_devices: set[str] = set()
        for group, records in groups.items():
            session_id = group[1]
            device_id = group[4]
            head = (
                await run_file_io(catalog.reader(session_id).head)
                if isinstance(session_id, str) and session_id
                else -1
            )
            if checked.get(group) == head:
                if isinstance(device_id, str) and device_id:
                    active_devices.add(device_id)
                continue
            async with ctx.runtime_scope():
                closed = await _try_end(ctx, bridge, catalog, projection, records, through_seq=head)
            if closed:
                _ = checked.pop(group, None)
            else:
                checked[group] = head
                if isinstance(device_id, str) and device_id:
                    active_devices.add(device_id)
        return active_devices

    async def reconcile_orphaned(active_devices: set[str]) -> None:
        if state is None:
            return
        targets = dict(pending_reconcile)
        pending_reconcile.clear()
        for descriptor in bridge.list_devices():
            if descriptor.virtual_display_id is not None or descriptor.pip_state in ("running", "needs_you"):
                session = bridge.current_session(descriptor.device_id)
                if session is not None:
                    targets.setdefault(descriptor.device_id, session.connection_id)
        for device_id, expected_conn_id in targets.items():
            if device_id in active_devices:
                continue

            async def check_still_orphaned(
                target_device: str = device_id,
                target_conn: str = expected_conn_id,
            ) -> None:
                session = bridge.current_session(target_device)
                if session is None or session.connection_id != target_conn:
                    raise _TurnSuperseded("连接已切换")
                assert state is not None
                for key, record in await run_file_io(state.list):
                    if (
                        key.startswith("phone-use:")
                        and record.value.get("phase") == "started"
                        and record.value.get("device_id") == target_device
                    ):
                        raise _TurnSuperseded("已有新 Turn 接管设备")

            try:
                async with ctx.runtime_scope():
                    _ = await bridge.finish_display(
                        device_id,
                        reason="orphaned_display_cleanup",
                        before_send=check_still_orphaned,
                    )
            except (_TurnSuperseded, PhoneNodeError):
                pass

    async with ctx.runtime_scope():
        active = await once()
        await reconcile_orphaned(active)

    follow_iter = catalog.follow().__aiter__()
    follow_task = asyncio.create_task(follow_iter.__anext__(), name="phone-follow-next")
    wake_task = asyncio.create_task(wake_reconcile.wait(), name="phone-wake-wait")
    try:
        while True:
            done, _ = await asyncio.wait((follow_task, wake_task), return_when=asyncio.FIRST_COMPLETED)
            if follow_task in done:
                try:
                    _ = follow_task.result()
                except StopAsyncIteration:
                    break
                follow_task = asyncio.create_task(follow_iter.__anext__(), name="phone-follow-next")
            if wake_task in done:
                wake_reconcile.clear()
                wake_task = asyncio.create_task(wake_reconcile.wait(), name="phone-wake-wait")
            async with ctx.runtime_scope():
                active = await once()
                await reconcile_orphaned(active)
    finally:
        for t in (follow_task, wake_task):
            if not t.done():
                _ = t.cancel()
        _ = await asyncio.gather(follow_task, wake_task, return_exceptions=True)


async def _try_end(
    ctx: Context,
    bridge: PhoneBridge,
    catalog: MessageCatalog,
    projection: TurnProjection,
    records: list[tuple[str, OwnerRecord]],
    *,
    through_seq: int,
) -> bool:
    key, record = records[0]
    value = record.value
    required = {"v", "phase", "control_binding", "session_id", "source", "turn_input_id", "device_id"}
    if (
        not required <= set(value)
        or value.get("v") != 1
        or value.get("phase") != "started"
        or any(
            not isinstance(value.get(field), str) or not value.get(field)
            for field in ("control_binding", "session_id", "source", "turn_input_id", "device_id")
        )
    ):
        await _mark_records(ctx, records, phase="failed", error="Phone owner record 字段无效")
        return True
    reader = catalog.reader(cast(str, value["session_id"]))
    source = cast(str, value["source"])
    turn_input_id = cast(str, value["turn_input_id"])
    closed = await run_file_io(
        lambda: _turn_closed(
            tuple(reader.committed_snapshot(through_seq=through_seq)),
            source,
            turn_input_id,
            projection,
        )
    )
    if not closed:
        return False
    # v1 回执可能含旧 keep/close 列表；保留原数据，但它们不再决定整屏收尾。
    binding = cast(str, value["control_binding"])
    device_id = cast(str, value["device_id"])
    identity = PhoneCallIdentity(
        cast(str, value["session_id"]),
        source,
        turn_input_id,
        device_id,
    )
    bindings = ctx.require(BINDINGS)
    try:
        async with bindings.open(binding, PHONE_CONTROL) as (bound, _):
            await bound.end_turn(identity)
    except (KeyError, ValueError):
        try:
            await ctx.require(PHONE_CONTROL).end_turn(identity)
        except Exception as error:
            ctx.report_incident("phone-end-turn", f"{key}: {error}")
            try:
                _ = await bridge.finish_display(device_id, reason="turn_ended_fallback")
            except PhoneNodeError:
                pass
    except Exception as error:
        ctx.report_incident("phone-end-turn", f"{key}: {error}")
        return True
    await _mark_records(ctx, records, phase="ended")
    return True


async def _mark_records(
    ctx: Context,
    records: list[tuple[str, OwnerRecord]],
    *,
    phase: str,
    error: str | None = None,
) -> None:
    state = ctx.require(OWNER_STATE).open(ctx)

    def commit(tx) -> None:
        for item_key, item_record in records:
            current = tx.read(item_key)
            if (
                current is None
                or current.version != item_record.version
                or current.value != item_record.value
            ):
                continue
            updated = {**item_record.value, "phase": phase}
            if error is not None:
                updated["error"] = error
            _ = tx.save(item_key, updated, expected_version=item_record.version)

    await state.transact_async(commit)
