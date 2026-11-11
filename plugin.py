"""Phone 端侧能力外置插件（phone-use）：按需加载（load_tools）、无线调试后台虚拟屏控制与 Turn 级生命周期。"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
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
    MessageCatalog,
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
    snapshot_to_parts,
)

PHONE_CONTROL = ServiceKey["PhoneControl"]("phone.control.v1")


@dataclass(frozen=True, slots=True)
class PhoneCallIdentity:
    session_id: str
    source: str
    turn_input_id: str
    device_id: str


@dataclass(frozen=True, slots=True)
class PhoneCleanupResult:
    outcome: Literal["confirmed", "not_sent", "unknown"]
    reason: str | None = None
    superseded: bool = False


class _TurnSuperseded(RuntimeError):
    """设备已经交给另一个 Turn；旧 Turn 不能再释放它。"""


class _TurnAlreadyFinished(RuntimeError):
    """设备回执已经确认收尾，恢复时不再发送。"""


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
        *,
        keep_packages: tuple[str, ...] = (),
        close_packages: tuple[str, ...] = (),
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
            "keep_packages": list(keep_packages),
            "close_packages": list(close_packages),
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
            timeout_ms=int(arguments.get("timeout_ms", 20_000)),
            before_send=lambda: self._record_started(key, control_binding, identity),
        )

    async def action(
        self,
        key: str,
        control_binding: str,
        arguments: Mapping[str, object],
    ) -> tuple[str, PhoneScreenSnapshot | None]:
        requested_device = cast(str | None, arguments.get("device_id"))
        device_id = self.bridge.resolve_device_id(requested_device)
        identity = _parse_call_identity(arguments, device_id)
        keep = tuple(str(x) for x in cast(list[object], arguments.get("keep_packages", [])) if isinstance(x, str))
        close = tuple(str(x) for x in cast(list[object], arguments.get("close_packages", [])) if isinstance(x, str))
        payload = {k: v for k, v in arguments.items() if k != "_phone" and v is not None}
        return await self.bridge.perform_action(
            device_id,
            payload,
            timeout_ms=int(arguments.get("timeout_ms", 30_000)),
            before_send=lambda: self._record_started(
                key, control_binding, identity, keep_packages=keep, close_packages=close,
            ),
        )

    async def end_turn(
        self,
        identity: PhoneCallIdentity,
        *,
        keep_packages: tuple[str, ...] = (),
        close_packages: tuple[str, ...] = (),
    ) -> PhoneCleanupResult:
        """仅当前 Turn 可收尾；未发送可在连接变化后重访，未知效果不重放。"""
        state = self._state()
        device_key = "phone-device:" + identity.device_id
        marker: OwnerRecord | None = None

        # 1. 发送锁内检查 owner，并先提交可跨换代保留的未知回执。
        async def check_owner() -> None:
            def prepare(tx):
                current = tx.read(device_key)
                if current is None:
                    raise PhoneNodeError("缺少设备当前 Turn 回执；收尾未发送，请观察设备后显式恢复")
                expected = (identity.session_id, identity.source, identity.turn_input_id)
                actual = tuple(current.value[field] for field in ("session_id", "source", "turn_input_id"))
                if actual != expected:
                    raise _TurnSuperseded("设备已由新 Turn 使用；旧 Turn 未发送收尾")
                if current.value.get("cleanup_outcome") == "confirmed":
                    raise _TurnAlreadyFinished()
                if current.value.get("cleanup_outcome") == "unknown":
                    raise PhoneNodeError("上次收尾结果未知，未自动重放；请观察设备后显式恢复", outcome="unknown")
                # 先记未知再发出，取消/进程退出不能把已发送的 finish 恢复成可重试。
                return tx.save(device_key, {**current.value, "cleanup_outcome": "unknown",
                                           "cleanup_reason": "收尾准备发送后尚未确认；取消或退出不能证明未执行"},
                               expected_version=current.version)

            def committed(record: OwnerRecord) -> None:
                nonlocal marker
                marker = record

            _ = await state.transact_async(prepare, on_commit=committed)

        # 2. 仅确定未发送允许以后重访；发送后的异常保留未知效果。
        try:
            _ = await self.bridge.finish_display(
                identity.device_id,
                keep_packages=keep_packages,
                close_packages=close_packages,
                reason="turn_ended",
                before_send=check_owner,
            )
        except _TurnSuperseded as error:
            return PhoneCleanupResult("not_sent", str(error), superseded=True)
        except _TurnAlreadyFinished:
            return PhoneCleanupResult("confirmed")
        except PhoneNodeError as error:
            result = PhoneCleanupResult(error.outcome, str(error))
        else:
            result = PhoneCleanupResult("confirmed")
        # 3. 新调用可能已接替设备，只结算仍属于本次发送的回执版本。
        if marker is not None and result.outcome != "unknown":
            def settle(tx) -> None:
                current = tx.read(device_key)
                if current is not None and current.version == marker.version:
                    _ = tx.save(device_key, {**current.value, "cleanup_outcome": result.outcome,
                                             "cleanup_reason": result.reason},
                                expected_version=current.version)
            await state.transact_async(settle)
        return result


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
            summary, snapshot = await self._control.action(key, self._control_binding, arguments)
        except PhoneNodeError as error:
            return Result("error", (ContentPart("text", str(error)),))
        if snapshot is None:
            return Result("success", (ContentPart("text", summary),))
        parts = await snapshot_to_parts(self._control.ctx, snapshot, prefix_text=summary)
        return Result("success", parts)

    async def query(self, key: str) -> Result | None:
        _ = key
        return None


api_version = 3
name = "phone-use"
version = "0.3.0"
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
    bridge = PhoneBridge()
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
            "own_focus / steal_top_focus_disabled）、右下角悬浮小窗状态以及是否等待用户主屏幕接管。"
            "在操作手机前先调用本工具确认设备在线。"
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
            "观察手机端后台虚拟屏幕（VirtualDisplay）当前的 Activity、结构化无障碍 UI 节点树（带 node_id 与中心坐标）"
            "以及可选的实时截图；可能初始化或唤醒虚拟屏。首次调用前先阅读 phone-use 技能。"
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
            "以及用户完成后收回后台虚拟屏幕（resume_from_user）。每次动作后返回更新后的虚拟屏快照；节点点击和文字输入须原样携带所属快照的 snapshot_ref。"
        ),
        parameters=PhoneActionInput.model_json_schema(),
        open=lambda state: _open_action(ctx, state),
        capture=lambda options: _capture(ctx, options),
        idempotent=False,
    )

    async def start_follower(_event: object) -> None:
        _ = await ctx.spawn(_start_follower(ctx), name="phone-turn-follower")

    _ = await ctx.on(RUNTIME_STARTED, start_follower)


async def _start_follower(ctx: Context) -> None:
    """扫描 started phone-use 记录，等待源 Message Turn 闭合后自动通知手机端收尾虚拟屏幕。"""
    catalog = ctx.require(MESSAGE_CATALOG)
    projection = ctx.require(TURN_PROJECTION)
    state: OwnerStore | None = None
    checked: dict[tuple[str, str, str, str, str], int] = {}

    async def once() -> None:
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
        for group, records in groups.items():
            session_id = group[1]
            head = (
                await run_file_io(catalog.reader(session_id).head)
                if isinstance(session_id, str) and session_id
                else -1
            )
            if checked.get(group) == head:
                continue
            async with ctx.runtime_scope():
                closed = await _try_end(ctx, catalog, projection, records, through_seq=head)
            if closed:
                _ = checked.pop(group, None)
            else:
                checked[group] = head

    wake = asyncio.Event()

    async def follow_messages() -> None:
        async for _heads in catalog.follow():
            wake.set()

    async def follow_connections() -> None:
        bridge = ctx.require(PHONE_BRIDGE)
        while True:
            await bridge.wait_connection_change()
            wake.set()

    # 连接变化不追加 Message；它只唤醒已有 not_sent 收尾，不靠定时轮询。
    async with asyncio.TaskGroup() as tasks:
        _ = tasks.create_task(follow_messages())
        _ = tasks.create_task(follow_connections())
        wake.set()
        while True:
            await wake.wait()
            wake.clear()
            async with ctx.runtime_scope():
                await once()


async def _try_end(
    ctx: Context,
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
    turns = await run_file_io(
        lambda: projection.project(
            tuple(reader.committed_snapshot(through_seq=through_seq)),
            cast(str, value["source"]),
        )
    )
    if not any(
        turn.status in {"complete", "quiet", "abandoned"}
        and cast(str, value["turn_input_id"]) in turn.message_ids
        for turn in turns
    ):
        return False
    keep: list[str] = []
    close: list[str] = []
    for _, rec in records:
        for pkg in cast(list[object], rec.value.get("keep_packages", [])):
            if isinstance(pkg, str) and pkg not in keep:
                keep.append(pkg)
        for pkg in cast(list[object], rec.value.get("close_packages", [])):
            if isinstance(pkg, str) and pkg not in close:
                close.append(pkg)
    binding = cast(str, value["control_binding"])
    device_id = cast(str, value["device_id"])
    bindings = ctx.require(BINDINGS)
    try:
        async with bindings.open(binding, PHONE_CONTROL) as (bound, _):
            result = await bound.end_turn(
                PhoneCallIdentity(cast(str, value["session_id"]), cast(str, value["source"]),
                                  cast(str, value["turn_input_id"]), device_id),
                keep_packages=tuple(keep), close_packages=tuple(close),
            )
    except (KeyError, ValueError) as error:
        await _mark_records(ctx, records, phase="failed", error=f"phone control binding 不可用: {error}")
        return True
    except Exception as error:
        ctx.report_incident("phone-end-turn", f"{key}: {error}")
        return True
    phase = "ended" if result.outcome == "confirmed" or result.superseded else (
        "cleanup_unknown" if result.outcome == "unknown" else "started"
    )
    await _mark_records(ctx, records, phase=phase, error=result.reason, cleanup_outcome=result.outcome)
    return True


async def _mark_records(
    ctx: Context,
    records: list[tuple[str, OwnerRecord]],
    *,
    phase: str,
    error: str | None = None,
    cleanup_outcome: str | None = None,
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
            if cleanup_outcome is not None:
                updated["cleanup_outcome"] = cleanup_outcome
                if error is None:
                    _ = updated.pop("error", None)
            _ = tx.save(item_key, updated, expected_version=item_record.version)

    await state.transact_async(commit)
