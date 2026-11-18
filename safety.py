"""端侧 UI 节点解析与快照格式化。"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from .contract import PhoneScreenSnapshot, PhoneUiNode

_STATE_LABELS = {
    "checked": "已选中",
    "unchecked": "未选中",
    "selected": "当前选中",
    "disabled": "不可用",
    "focused": "聚焦",
    "long_press": "可长按",
}
_SCROLL_LABELS = {
    "up": "上方还有",
    "down": "下方还有",
    "left": "左侧还有",
    "right": "右侧还有",
    "backward": "可向前滚",
    "forward": "可向后滚",
}
_BLANK_RUN_MIN = 4
_SCREENSHOT_REASONS = {
    "requested": "按要求附上",
    "sparse_tree": "界面几乎读不到控件文字（自绘、网页或应用屏蔽了无障碍），已自动附上截图",
    "no_change": "上一步之后界面没有变化，已自动附上截图供核对",
}


def parse_ui_nodes(raw_nodes: object) -> tuple[PhoneUiNode, ...]:
    """将端侧上报的 JSON 节点列表解析为结构化不可变节点；兼容只带旧字段的 Shell。"""
    if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)):
        return ()
    nodes: list[PhoneUiNode] = []
    for index, item in enumerate(raw_nodes, start=1):
        if not isinstance(item, Mapping):
            continue
        node_id = item.get("ref", item.get("node_id", index))
        if not isinstance(node_id, int) or isinstance(node_id, bool) or node_id <= 0:
            node_id = index
        text = str(item.get("text") or "")
        desc = str(item.get("content_description") or "")
        checked = item.get("checked")
        value = item.get("value")
        depth = item.get("depth", 0)
        nodes.append(
            PhoneUiNode(
                node_id=node_id,
                class_name=str(item.get("class_name") or "android.view.View"),
                text=text,
                content_description=desc,
                bounds=_bounds(item.get("bounds")),
                clickable=bool(item.get("clickable", False)),
                editable=bool(item.get("editable", False)),
                scrollable=bool(item.get("scrollable", False)),
                focused=bool(item.get("focused", False)),
                checked=checked if isinstance(checked, bool) else None,
                depth=depth if isinstance(depth, int) and not isinstance(depth, bool) and depth >= 0 else 0,
                role=str(item.get("role") or ""),
                name=str(item.get("name") or " / ".join(part for part in (text, desc) if part)),
                resource_id=str(item.get("resource_id") or ""),
                value=value if isinstance(value, str) else None,
                hint=str(item.get("hint") or ""),
                states=_strings(item.get("states")),
                scroll=_strings(item.get("scroll")),
            )
        )
    return tuple(nodes)


def format_screen_snapshot(snapshot: PhoneScreenSnapshot, app_label: str | None = None) -> str:
    """渲染对标 ego-lite 的紧凑快照：按层级缩进、一行一个元素、带跨快照稳定的 @编号，不带坐标。"""
    app = f"{app_label}（{snapshot.package_name}）" if app_label else (snapshot.package_name or "未知应用")
    activity = snapshot.activity_name.rsplit(".", 1)[-1] if snapshot.activity_name else ""
    lines = [f"{snapshot.device_id} 后台屏 #{snapshot.display_id} · {app}" + (f" · {activity}" if activity else "")]
    if snapshot.takeover_reason:
        lines.append(f"TAKEOVER REQUIRED: {snapshot.takeover_reason}")
        lines.append("端侧已明确请求用户接管；可先观察状态，用户完成后调用 resume_from_user。")
    if snapshot.secure_window:
        lines.append("CAPTURE LIMITED: 设备报告 FLAG_SECURE，截图可能受限；这不代表用户已请求接管。")
    if not snapshot.nodes:
        lines.append("界面元素：（没有读到任何控件）")
    else:
        lines.append('界面元素（target 可写 "@编号"、可见文字或 "id=资源名"；@编号跨快照有效）：')
        lines.extend(_node_lines(snapshot.nodes, has_image=snapshot.screenshot_png is not None))
        if snapshot.truncated:
            lines.append("…（元素过多，后面的未列出；滚动或用文字定位）")
    if snapshot.screenshot_reason:
        lines.append(f"截图：{_SCREENSHOT_REASONS.get(snapshot.screenshot_reason, snapshot.screenshot_reason)}")
    elif not snapshot.nodes:
        lines.append("截图：未附上；需要看图时设 include_screenshot=true")
    return "\n".join(lines)


# 自绘界面常有成片无文字的可点元素（网格、卡片），连续同层同角色的合成一行，看截图按坐标操作。
def _node_lines(nodes: Sequence[PhoneUiNode], *, has_image: bool) -> list[str]:
    advice = "看截图按坐标操作" if has_image else "需要时 include_screenshot=true 看图"
    lines: list[str] = []
    index = 0
    while index < len(nodes):
        node = nodes[index]
        end = index
        while end + 1 < len(nodes) and _blank_run(nodes[end + 1], node):
            end += 1
        if end - index + 1 >= _BLANK_RUN_MIN and _blank_run(node, node):
            role = node.role or _legacy_role(node)
            lines.append(
                f"{'  ' * node.depth}- {role} ×{end - index + 1} 无文字 @{node.node_id}…@{nodes[end].node_id}（{advice}）"
            )
        else:
            lines.extend(_node_line(item) for item in nodes[index:end + 1])
        index = end + 1
    return lines


def _blank_run(node: PhoneUiNode, first: PhoneUiNode) -> bool:
    return (
        not node.name and not node.value and not node.states and not node.scroll
        and node.depth == first.depth and (node.role or _legacy_role(node)) == (first.role or _legacy_role(first))
    )


def _node_line(node: PhoneUiNode) -> str:
    role = node.role or _legacy_role(node)
    parts = [f"{'  ' * node.depth}- {role}"]
    if node.name:
        parts.append(_quote(node.name))
    if node.value:
        parts.append(f"= {_quote(node.value)}")
    parts.append(f"@{node.node_id}")
    flags = [_STATE_LABELS.get(state, state) for state in node.states]
    flags.extend(_SCROLL_LABELS.get(item, item) for item in node.scroll)
    if node.resource_id:
        flags.append(f"id={node.resource_id}")
    if flags:
        parts.append(f"[{'，'.join(flags)}]")
    return " ".join(parts)


def _legacy_role(node: PhoneUiNode) -> str:
    if node.editable:
        return "textbox"
    if node.scrollable:
        return "list"
    if node.clickable:
        return "button"
    return "text"


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _bounds(raw: object) -> tuple[int, int, int, int]:
    if (
        isinstance(raw, Sequence)
        and not isinstance(raw, (str, bytes))
        and len(raw) == 4
        and all(isinstance(v, (int, float)) for v in raw)
    ):
        return (int(raw[0]), int(raw[1]), int(raw[2]), int(raw[3]))
    return (0, 0, 0, 0)


def _strings(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(item for item in raw if isinstance(item, str) and item)
