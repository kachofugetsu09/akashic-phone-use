"""Phone 端侧执行器公共合同：在线设备快照、UI 节点树与后台虚拟屏动作请求。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from agent.plugin_composition.model import ServiceKey

PhoneDisplayMode = Literal["virtual_display", "accessibility_only", "pairing_required"]
PhonePipState = Literal["hidden", "running", "needs_you", "done"]
PhoneActionKind = Literal[
    "launch_app",
    "tap",
    "type_text",
    "clear_text",
    "scroll",
    "swipe",
    "press_key",
    "back",
    "open_link",
    "share_text",
    "wait",
    "hand_to_user",
    "resume_from_user",
]


@dataclass(frozen=True, slots=True)
class PhoneUiNode:
    """后台虚拟屏上的单个无障碍/OCR 可交互元素。"""

    node_id: int
    class_name: str
    text: str
    content_description: str
    bounds: tuple[int, int, int, int]
    clickable: bool = False
    editable: bool = False
    scrollable: bool = False
    focused: bool = False
    checked: bool | None = None

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return ((left + right) // 2, (top + bottom) // 2)


@dataclass(frozen=True, slots=True)
class PhoneScreenSnapshot:
    """一次端侧观察返回的虚拟屏状态、UI 树与可选 PNG 帧。"""

    device_id: str
    snapshot_ref: str
    display_id: int
    width: int
    height: int
    rotation: int
    package_name: str  # 空串表示本次观察无法确定。
    activity_name: str
    pip_state: PhonePipState
    audio_muted: bool
    nodes: tuple[PhoneUiNode, ...]
    screenshot_png: bytes | None = None
    takeover_reason: str | None = None
    secure_window: bool = False


@dataclass(frozen=True, slots=True)
class PhoneDeviceDescriptor:
    """当前在线的端侧手机节点摘要。"""

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
    takeover_reason: str | None = None


class PhoneBridgePort(Protocol):
    """供测试、诊断或兄弟插件只读查询当前在线设备。"""

    def list_devices(self) -> tuple[PhoneDeviceDescriptor, ...]: ...


PHONE_BRIDGE = ServiceKey[PhoneBridgePort]("phone.bridge.v1")

__all__ = [
    "PHONE_BRIDGE",
    "PhoneActionKind",
    "PhoneBridgePort",
    "PhoneDeviceDescriptor",
    "PhoneDisplayMode",
    "PhonePipState",
    "PhoneScreenSnapshot",
    "PhoneUiNode",
]
