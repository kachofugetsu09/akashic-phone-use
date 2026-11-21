"""Phone 工具组定义：按需经 load_tools 解锁，零前缀缓存污染。"""
from __future__ import annotations

import difflib
import json
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from core.common.file_io import run_file_io
from plugins.ledger.contract import (
    ARTIFACT_IMPORT,
    AttachmentKind,
    ContentPart,
    Input,
    Message,
    Output,
    ToolCall,
    json_value,
)
from plugins.tools.contract import CallSource, Result
from plugins.turn_projection.contract import TurnProjection

from .bridge import PhoneBridge, PhoneNodeError
from .contract import PhoneActionKind, PhoneApp, PhoneScreenSnapshot
from .events import (
    AdapterKind,
    PhoneEventStore,
    PhoneSubscriptionRule,
    RuleActionsConfig,
    RuleMatchConfig,
)
from .safety import format_screen_snapshot


class PhoneDevicesInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PhoneObserveInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    device_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="目标手机设备 ID；仅一台手机在线时可省略。",
    )
    include_screenshot: bool = Field(
        default=False,
        description="强制附上截图。默认不需要：界面读不到控件文字时会自动附图，只有要看颜色、图片、布局等视觉细节时才设 true。",
    )
    timeout_ms: int = Field(default=20_000, ge=100, le=110_000)


_ACTION_DOC = (
    "动作类型：launch_app（在后台虚拟屏启动应用）、tap（按 target 点击，界面读不到控件时才用 x/y）、"
    "type_text（设置输入框全文，覆盖原有内容，无需先清空；需检查返回结果与字段内容）、"
    "clear_text、scroll（up/down/left/right）、swipe、press_key、back、"
    "open_link、share_text、wait、hand_to_user（按用户要求将应用移交手机主屏幕）、"
    "resume_from_user（用户完成主屏幕操作后收回后台虚拟屏继续）。"
)


class _StepFields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    app: str | None = Field(
        default=None, min_length=1, max_length=100,
        description="launch_app 的目标：应用显示名（如 \"支付宝\"、\"Clash\"）或包名；不必先查包名。",
    )
    package_name: str | None = Field(default=None, min_length=1, max_length=256)
    target: str | None = Field(
        default=None, min_length=1, max_length=200,
        description=(
            '要操作的元素：快照里的 "@12"（同一控件跨快照不变，可直接沿用）、可见文字如 "我的"'
            '（忽略大小写的子串匹配；两端再加一层引号则精确匹配）、或 "id=search_box"（resource-id）。'
            "tap / type_text / clear_text / scroll 可用；匹配到多个时会列出候选。"
        ),
    )
    node_id: int | None = Field(default=None, ge=1, description="旧写法，等同 target=\"@node_id\"。")
    x: int | None = Field(default=None, ge=0, le=10_000)
    y: int | None = Field(default=None, ge=0, le=10_000)
    end_x: int | None = Field(default=None, ge=0, le=10_000)
    end_y: int | None = Field(default=None, ge=0, le=10_000)
    duration_ms: int = Field(default=300, ge=10, le=10_000)
    text: str | None = Field(default=None, max_length=16_384)
    direction: Literal["up", "down", "left", "right"] | None = None
    key_code: str | None = Field(default=None, min_length=1, max_length=64)
    uri: str | None = Field(default=None, min_length=1, max_length=4096)
    wait_for: str | None = Field(
        default=None, min_length=1, max_length=100,
        description="动作后等待界面出现含这段文字的元素（最多约 8 秒），用于加载慢的页面；出现即返回。",
    )



class PhoneStepInput(_StepFields):
    action: PhoneActionKind = Field(description=_ACTION_DOC)

    @model_validator(mode="after")
    def _validate_step(self) -> PhoneStepInput:
        _check_action_fields(self, self.action)
        return self


class PhoneActionInput(_StepFields):
    description: str = Field(
        min_length=1,
        max_length=60,
        description="简短中文说明本步操作，例如：在后台虚拟屏打开微信、点击搜索框。",
    )
    action: PhoneActionKind | None = Field(default=None, description=_ACTION_DOC)
    steps: list[PhoneStepInput] | None = Field(
        default=None, min_length=1, max_length=10,
        description=(
            "一次顺序执行多步（与 action 二选一），如 打开应用→点搜索→输入。任一步失败即停下，"
            "返回失败原因与当时的界面；只返回最后一步的快照。下一步依赖本步结果才能决定时不要合并。"
        ),
    )
    device_id: str | None = Field(default=None, min_length=1, max_length=128)
    snapshot_ref: str | None = Field(default=None, min_length=1, max_length=128, description="已不需要，可省略。")
    reason: str | None = Field(default=None, min_length=1, max_length=500)
    include_screenshot: bool = Field(
        default=False,
        description="强制附上截图。默认不需要：界面读不到控件文字或动作后界面没变化时会自动附图。",
    )
    timeout_ms: int = Field(default=30_000, ge=100, le=110_000)

    @model_validator(mode="after")
    def _validate_request(self) -> PhoneActionInput:
        if (self.action is None) == (self.steps is None):
            raise ValueError("action 与 steps 必须且只能提供一个")
        stray = sorted(self.model_fields_set & set(_StepFields.model_fields)) if self.steps is not None else []
        if stray:
            raise ValueError(f"使用 steps 时 {', '.join(stray)} 要写进各步里，不能放在顶层")
        if self.action is not None:
            _check_action_fields(self, self.action)
        if self.action == "hand_to_user" and not self.reason:
            raise ValueError("hand_to_user 必须提供 reason 说明移交原因")
        for index, step in enumerate(self.steps or (), start=1):
            if step.action in ("hand_to_user", "resume_from_user"):
                raise ValueError(f"第 {index} 步：{step.action} 需单独调用，不能放进 steps")
        return self


def _check_action_fields(step: _StepFields, act: PhoneActionKind) -> None:
    if step.target is not None and step.node_id is not None:
        raise ValueError("target 与 node_id 只能二选一")
    if step.app is not None and step.package_name is not None:
        raise ValueError("app 与 package_name 只能二选一")
    if step.target is not None:
        _ = parse_target(step.target)
    if act == "launch_app" and not (step.app or step.package_name):
        raise ValueError("launch_app 必须提供 app（应用名或包名）")
    if act == "tap" and step.target is None and step.node_id is None and (step.x is None or step.y is None):
        raise ValueError("tap 必须提供 target，或在界面读不到控件时同时提供 (x, y) 坐标")
    if act == "type_text" and (step.text is None or step.text == ""):
        raise ValueError("type_text 必须提供非空 text")
    if act == "scroll" and step.direction is None:
        raise ValueError("scroll 必须提供 direction (up/down/left/right)")
    if act == "swipe" and any(v is None for v in (step.x, step.y, step.end_x, step.end_y)):
        raise ValueError("swipe 必须提供起点 (x, y) 与终点 (end_x, end_y)")
    if act == "press_key" and not step.key_code:
        raise ValueError("press_key 必须提供 key_code")
    if act == "open_link" and not step.uri:
        raise ValueError("open_link 必须提供 uri")
    if act == "share_text" and (not (step.package_name or step.app) or not step.text):
        raise ValueError("share_text 必须同时提供目标应用（app 或 package_name）与 text")


def resolve_app(apps: Sequence[PhoneApp], query: str) -> str:
    """按显示名或包名找唯一应用：精确 > 前缀 > 包含；找不到或不唯一时给出候选。"""
    wanted = query.strip().casefold()
    by_package = [app for app in apps if app.package.casefold() == wanted]
    if by_package:
        return by_package[0].package
    for rule in (
        lambda label: label == wanted,
        lambda label: label.startswith(wanted),
        lambda label: wanted in label,
    ):
        hits = [app for app in apps if rule(app.label.casefold())]
        if len(hits) == 1:
            return hits[0].package
        if len(hits) > 1:
            listed = "；".join(f"{app.label}（{app.package}）" for app in hits[:8])
            raise ValueError(f"\"{query}\" 匹配到多个应用：{listed}，请写全名或包名")
    if "." in wanted and " " not in wanted:
        return query.strip()
    close = difflib.get_close_matches(wanted, [app.label.casefold() for app in apps], n=5, cutoff=0.4)
    names = [app.label for app in apps if app.label.casefold() in close]
    hint = f"；相近的有：{'、'.join(names)}" if names else "；可用 phone_devices 查看已装应用"
    raise ValueError(f"手机上没有名为 \"{query}\" 的应用{hint}")


def format_apps(apps: Sequence[PhoneApp]) -> str:
    return "、".join(app.label for app in sorted(apps, key=lambda app: app.label.casefold()))


def parse_target(target: str) -> dict[str, object]:
    """把 target 写法转成端侧线格式：@编号走 node_id，其余走语义 selector。"""
    raw = target.strip()
    if raw.startswith("@") and raw[1:].isdigit() and int(raw[1:]) > 0:
        return {"node_id": int(raw[1:])}
    if raw.startswith("id="):
        resource_id = raw[3:].strip()
        if not resource_id:
            raise ValueError("id= 后面需要资源名")
        return {"selector": {"resource_id": resource_id}}
    text = raw[5:].strip() if raw.startswith("text=") else raw
    exact = len(text) >= 2 and text[0] == text[-1] and text[0] in ("\"", "'")
    if exact:
        text = text[1:-1]
    if not text:
        raise ValueError("target 不能为空")
    return {"selector": {"text": text, "exact": exact}}


def extract_turn_identity(
    source: CallSource | None, projection: TurnProjection
) -> dict[str, str]:
    """提取已提交 Message 的稳定 Turn 身份，供 Turn 闭合自动释放虚拟屏。"""
    if source is None:
        raise ValueError("Phone 工具只能由已提交 Message 调用")
    target = _call_message(source)
    turns = projection.project(source.messages, target.source)
    matched = [
        turn for turn in turns if turn.status == "open" and target.message_id in turn.message_ids
    ]
    if len(matched) != 1:
        raise ValueError("Phone ToolCall 不属于唯一 open Turn")
    by_id = {message.message_id: message for message in source.messages}
    inputs = [
        message
        for message in (by_id[item] for item in matched[0].message_ids)
        if isinstance(message.body, Input) and message.seq <= target.seq
    ]
    if not inputs:
        raise ValueError("Phone Turn 缺少前置 Input")
    return {
        "session_id": target.session_id,
        "source": target.source,
        "turn_input_id": inputs[0].message_id,
    }


def _call_message(source: CallSource) -> Message:
    target = next(
        (message for message in source.messages if message.message_id == source.call_ref.message_id),
        None,
    )
    if target is None or not isinstance(target.body, Output):
        raise ValueError("Phone CallRef 缺少 Output")
    if source.call_ref.part_index >= len(target.body.parts):
        raise ValueError("Phone CallRef 超出 Output")
    if not isinstance(target.body.parts[source.call_ref.part_index], ToolCall):
        raise ValueError("Phone CallRef 不指向 ToolCall")
    if target.seq != source.messages[-1].seq:
        raise ValueError("Phone CallRef 不是 source 前缀末端")
    return target


async def snapshot_to_parts(
    ctx,
    snapshot: PhoneScreenSnapshot,
    *,
    prefix_text: str = "",
) -> tuple[ContentPart, ...]:
    text = format_screen_snapshot(snapshot)
    if prefix_text:
        text = f"{prefix_text}\n\n{text}"
    parts: list[ContentPart] = [ContentPart("text", text)]
    if snapshot.screenshot_png:
        with TemporaryDirectory(prefix="akashic-phone-image-") as folder:
            path = Path(folder) / "phone_screen.png"
            raw = snapshot.screenshot_png
            _ = await run_file_io(lambda: path.write_bytes(raw))
            ref = await ctx.require(ARTIFACT_IMPORT).import_source(str(path), AttachmentKind.IMAGE)
        if ref.kind is not AttachmentKind.IMAGE:
            raise ValueError("Phone 截图返回的附件类型不是 IMAGE")
        parts.append(ContentPart("artifact_ref", ref.artifact_id))
    return tuple(parts)


class PhoneDevicesTool:
    """列出当前通过无线调试/Shell Bridge 连入的在线手机设备。"""

    idempotent = True

    def __init__(self, bridge: PhoneBridge) -> None:
        self._bridge = bridge

    async def prepare(
        self, arguments: Mapping[str, object], source: CallSource | None = None
    ) -> Mapping[str, object] | str:
        _ = source
        try:
            return PhoneDevicesInput.model_validate(json_value(arguments)).model_dump(mode="json")
        except ValidationError as error:
            return str(error)

    async def invoke(self, key: str, arguments: Mapping[str, object]) -> Result:
        _ = (key, arguments)
        devices = self._bridge.list_devices()
        if not devices:
            return Result(
                "success",
                (
                    ContentPart(
                        "text",
                        "当前没有在线的 Android 手机端侧设备（0 devices online）。"
                        "请确认手机上的 Akashic Shell 已连接服务器并完成无线调试后台控制配对。",
                    ),
                ),
            )
        lines = [f"Online Android Devices ({len(devices)}):"]
        app_lines: list[str] = []
        for dev in devices:
            vd = f"#{dev.virtual_display_id}" if dev.virtual_display_id is not None else "lazy (not created yet)"
            focus_info = (
                "own_focus=true, steal_top_focus_disabled=true"
                if dev.own_focus and dev.steal_top_focus_disabled
                else f"own_focus={dev.own_focus}, steal_top_focus_disabled={dev.steal_top_focus_disabled}"
            )
            pkgs = ", ".join(dev.active_packages) if dev.active_packages else "none"
            line = (
                f"- device_id={dev.device_id!r} ({dev.device_name}, {dev.model}, Android SDK {dev.sdk_int}, "
                f"Shell {dev.shell_version}) | mode={dev.display_mode} | virtual_display={vd} ({focus_info}) | "
                f"pip={dev.pip_state} | active_packages=[{pkgs}]"
            )
            if dev.takeover_reason:
                line += f" | TAKEOVER_PENDING={dev.takeover_reason!r}"
            lines.append(line)
            if self._bridge.supports_apps(dev.device_id):
                try:
                    apps = await self._bridge.launcher_apps(dev.device_id, refresh=True)
                except PhoneNodeError as error:
                    app_lines.append(f"{dev.device_id} 已装应用：暂时读不到（{error}）")
                else:
                    app_lines.append(
                        f"{dev.device_id} 已装应用（{len(apps)} 个，launch_app 直接写 app=显示名）：{format_apps(apps)}"
                    )
        return Result("success", (ContentPart("text", "\n".join([*lines, *app_lines])),))

    async def query(self, key: str) -> Result | None:
        _ = key
        return None


class PhoneEventsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False)
    action: Literal["recent_events", "app_sessions", "current_state"] = Field(
        default="recent_events",
        description="recent_events（查询最近事件）、app_sessions（前台应用会话时长）、current_state（最近一次前台/媒体/设备状态）",
    )
    adapter: AdapterKind | None = Field(default=None, description="按适配器过滤：notification / foreground / appops / media / device")
    package_name: str | None = Field(default=None, min_length=1, max_length=256)
    device_id: str | None = Field(default=None, min_length=1, max_length=128)
    since_hours: float = Field(default=24.0, gt=0, le=720.0)
    limit: int = Field(default=30, ge=1, le=100)


class DeviceSubscribeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False)
    action: Literal["list", "upsert", "remove", "enable", "disable"] = Field(
        description="list（查看规则）、upsert（新增或覆盖规则并立即下发手机）、remove（删除规则）、enable/disable（启停规则）"
    )
    rule_id: str | None = Field(default=None, min_length=1, max_length=100, description="remove / enable / disable 时必填；upsert 时可写在 rule.id")
    rule: PhoneSubscriptionRule | None = Field(default=None, description="upsert 时的完整订阅规则对象")
    ttl_seconds: int | None = Field(default=None, ge=60, le=30 * 86_400, description="可选：给 upsert 规则设置有效期（秒），到期自动失效")

    @model_validator(mode="after")
    def _validate_subscribe(self) -> DeviceSubscribeInput:
        if self.action == "upsert" and self.rule is None:
            raise ValueError("upsert 必须提供 rule 对象")
        if self.action in ("remove", "enable", "disable") and not self.rule_id:
            raise ValueError(f"{self.action} 必须提供 rule_id")
        return self


class PhoneEventsTool:
    """按需只读查询手机端侧采集到的事件流与最新状态。"""

    idempotent = True

    def __init__(self, store: PhoneEventStore) -> None:
        self._store = store

    async def prepare(
        self, arguments: Mapping[str, object], source: CallSource | None = None
    ) -> Mapping[str, object] | str:
        _ = source
        try:
            return PhoneEventsInput.model_validate(json_value(arguments)).model_dump(mode="json")
        except ValidationError as error:
            return str(error)

    async def invoke(self, key: str, arguments: Mapping[str, object]) -> Result:
        _ = key
        action = str(arguments.get("action") or "recent_events")
        if action == "recent_events":
            data = await run_file_io(
                lambda: self._store.query_recent_events(
                    adapter=arguments.get("adapter") if isinstance(arguments.get("adapter"), str) else None,
                    package_name=arguments.get("package_name") if isinstance(arguments.get("package_name"), str) else None,
                    device_id=arguments.get("device_id") if isinstance(arguments.get("device_id"), str) else None,
                    since_hours=float(arguments.get("since_hours") or 24.0),
                    limit=int(arguments.get("limit") or 30),
                )
            )
        elif action == "app_sessions":
            data = await run_file_io(
                lambda: self._store.query_app_sessions(
                    package_name=arguments.get("package_name") if isinstance(arguments.get("package_name"), str) else None,
                    device_id=arguments.get("device_id") if isinstance(arguments.get("device_id"), str) else None,
                    since_hours=float(arguments.get("since_hours") or 24.0),
                    limit=int(arguments.get("limit") or 30),
                )
            )
        else:
            data = await run_file_io(
                lambda: self._store.query_current_state(
                    device_id=arguments.get("device_id") if isinstance(arguments.get("device_id"), str) else None,
                )
            )
        return Result("success", (ContentPart("text", json.dumps(data, ensure_ascii=False, indent=2)),))

    async def query(self, key: str) -> Result | None:
        _ = key
        return None


class DeviceSubscribeTool:
    """动态管理手机端侧事件订阅规则，变更后立即持久化并推送到在线手机。"""

    idempotent = False

    def __init__(self, store: PhoneEventStore, bridge: PhoneBridge) -> None:
        self._store = store
        self._bridge = bridge

    async def prepare(
        self, arguments: Mapping[str, object], source: CallSource | None = None
    ) -> Mapping[str, object] | str:
        _ = source
        try:
            return DeviceSubscribeInput.model_validate(json_value(arguments)).model_dump(mode="json")
        except ValidationError as error:
            return str(error)

    async def invoke(self, key: str, arguments: Mapping[str, object]) -> Result:
        _ = key
        action = str(arguments["action"])
        if action == "list":
            payload = await run_file_io(self._store.list_rules)
            return Result("success", (ContentPart("text", json.dumps(payload, ensure_ascii=False, indent=2)),))

        if action == "upsert":
            rule = PhoneSubscriptionRule.model_validate(arguments["rule"])
            ttl = arguments.get("ttl_seconds")
            if isinstance(ttl, (int, float)) and not isinstance(ttl, bool):
                rule = rule.model_copy(update={"expires_at": time.time() + float(ttl)})
            payload = await run_file_io(lambda: self._store.upsert_rule(rule))
        elif action == "remove":
            rule_id = str(arguments["rule_id"])
            found, payload = await run_file_io(lambda: self._store.remove_rule(rule_id))
            if not found:
                return Result("error", (ContentPart("text", f"未找到订阅规则 {rule_id!r}"),))
        else:
            rule_id = str(arguments["rule_id"])
            enabled = action == "enable"
            found, payload = await run_file_io(lambda: self._store.set_rule_enabled(rule_id, enabled))
            if not found:
                return Result("error", (ContentPart("text", f"未找到订阅规则 {rule_id!r}"),))

        pushed = await self._bridge.push_rules(payload)
        summary = {
            "ok": True,
            "pushed_devices": pushed,
            "rules_version": payload["rules_version"],
            "rules": payload["rules"],
        }
        return Result("success", (ContentPart("text", json.dumps(summary, ensure_ascii=False, indent=2)),))

    async def query(self, key: str) -> Result | None:
        _ = key
        return None
