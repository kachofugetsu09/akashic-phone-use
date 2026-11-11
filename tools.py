"""Phone 工具组定义：按需经 load_tools 解锁，零前缀缓存污染。"""
from __future__ import annotations

from collections.abc import Mapping
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
from .contract import PhoneActionKind, PhoneScreenSnapshot
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
        description="是否同时抓取后台虚拟屏当前 PNG 截图并存为图像附件（无障碍树不足以判断或需看图时设为 true）。",
    )
    timeout_ms: int = Field(default=20_000, ge=100, le=110_000)


class PhoneActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    description: str = Field(
        min_length=1,
        max_length=60,
        description="简短中文说明本步操作，例如：在后台虚拟屏打开微信、点击搜索框。",
    )
    action: PhoneActionKind = Field(
        description=(
            "动作类型：launch_app（在后台虚拟屏启动应用）、tap（按 node_id 或 x/y 点击）、"
            "type_text（设置输入框全文，覆盖原有内容，无需先清空；需检查返回结果与字段内容）、"
            "clear_text、scroll（up/down/left/right）、swipe、press_key、back、"
            "open_link、share_text、wait、hand_to_user（按用户要求将应用移交手机主屏幕）、"
            "resume_from_user（用户完成主屏幕操作后收回后台虚拟屏继续）。"
        )
    )
    device_id: str | None = Field(default=None, min_length=1, max_length=128)
    package_name: str | None = Field(default=None, min_length=1, max_length=256)
    node_id: int | None = Field(default=None, ge=1)
    snapshot_ref: str | None = Field(
        default=None, min_length=1, max_length=128,
        description="节点所属快照的 snapshot_ref，原样复制最近一次工具返回值；节点点击及 type_text/clear_text 必填，不能从旧快照借用。",
    )
    x: int | None = Field(default=None, ge=0, le=10_000)
    y: int | None = Field(default=None, ge=0, le=10_000)
    end_x: int | None = Field(default=None, ge=0, le=10_000)
    end_y: int | None = Field(default=None, ge=0, le=10_000)
    duration_ms: int = Field(default=300, ge=10, le=10_000)
    text: str | None = Field(default=None, max_length=16_384)
    direction: Literal["up", "down", "left", "right"] | None = None
    key_code: str | None = Field(default=None, min_length=1, max_length=64)
    uri: str | None = Field(default=None, min_length=1, max_length=4096)
    reason: str | None = Field(default=None, min_length=1, max_length=500)
    include_screenshot: bool = Field(
        default=False,
        description="动作执行后是否附带最新一帧后台虚拟屏 PNG 截图。",
    )
    timeout_ms: int = Field(default=30_000, ge=100, le=110_000)

    @model_validator(mode="after")
    def _validate_action_fields(self) -> PhoneActionInput:
        act = self.action
        if (act in ("type_text", "clear_text") or (act == "tap" and self.node_id is not None)) and not self.snapshot_ref:
            raise ValueError("节点操作必须提供所属快照的 snapshot_ref；请先观察，不自动使用最新引用")
        if act == "launch_app" and not self.package_name:
            raise ValueError("launch_app 必须提供 package_name")
        if act == "tap" and self.node_id is None and (self.x is None or self.y is None):
            raise ValueError("tap 必须提供 node_id 或同时提供 (x, y) 坐标")
        if act == "type_text" and (self.text is None or self.text == ""):
            raise ValueError("type_text 必须提供非空 text")
        if act == "scroll" and self.direction is None:
            raise ValueError("scroll 必须提供 direction (up/down/left/right)")
        if act == "swipe" and any(v is None for v in (self.x, self.y, self.end_x, self.end_y)):
            raise ValueError("swipe 必须提供起点 (x, y) 与终点 (end_x, end_y)")
        if act == "press_key" and not self.key_code:
            raise ValueError("press_key 必须提供 key_code")
        if act == "open_link" and not self.uri:
            raise ValueError("open_link 必须提供 uri")
        if act == "share_text" and (not self.package_name or not self.text):
            raise ValueError("share_text 必须同时提供 package_name 与 text")
        if act == "hand_to_user" and not self.reason:
            raise ValueError("hand_to_user 必须提供 reason 说明移交原因")
        return self


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
        return Result("success", (ContentPart("text", "\n".join(lines)),))

    async def query(self, key: str) -> Result | None:
        _ = key
        return None
