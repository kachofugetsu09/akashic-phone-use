"""端侧 UI 节点解析与快照格式化。"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from .contract import PhoneScreenSnapshot, PhoneUiNode


def parse_ui_nodes(raw_nodes: object) -> tuple[PhoneUiNode, ...]:
    """将端侧上报的 JSON 节点列表解析为结构化不可变节点。"""
    if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)):
        return ()
    nodes: list[PhoneUiNode] = []
    for index, item in enumerate(raw_nodes, start=1):
        if not isinstance(item, Mapping):
            continue
        raw_bounds = item.get("bounds", (0, 0, 0, 0))
        if (
            isinstance(raw_bounds, Sequence)
            and not isinstance(raw_bounds, (str, bytes))
            and len(raw_bounds) == 4
            and all(isinstance(v, (int, float)) for v in raw_bounds)
        ):
            bounds = (int(raw_bounds[0]), int(raw_bounds[1]), int(raw_bounds[2]), int(raw_bounds[3]))
        else:
            bounds = (0, 0, 0, 0)
        node_id = item.get("node_id", index)
        if not isinstance(node_id, int) or isinstance(node_id, bool) or node_id <= 0:
            node_id = index
        checked = item.get("checked")
        nodes.append(
            PhoneUiNode(
                node_id=node_id,
                class_name=str(item.get("class_name") or "android.view.View"),
                text=str(item.get("text") or ""),
                content_description=str(item.get("content_description") or ""),
                bounds=bounds,
                clickable=bool(item.get("clickable", False)),
                editable=bool(item.get("editable", False)),
                scrollable=bool(item.get("scrollable", False)),
                focused=bool(item.get("focused", False)),
                checked=checked if isinstance(checked, bool) else None,
            )
        )
    return tuple(nodes)


def format_screen_snapshot(snapshot: PhoneScreenSnapshot) -> str:
    """将后台虚拟屏快照渲染为高信噪比、省 Token 的文本表示。"""
    header = [
        f"Snapshot: snapshot_ref={snapshot.snapshot_ref}",
        f"Device: {snapshot.device_id} | VirtualDisplay: #{snapshot.display_id} ({snapshot.width}x{snapshot.height} rot={snapshot.rotation})",
        f"Foreground: {snapshot.package_name or 'unknown'}/{snapshot.activity_name or 'unknown'} | PiP: {snapshot.pip_state} | AudioMuted: {snapshot.audio_muted}",
    ]
    if snapshot.takeover_reason:
        header.append(f"TAKEOVER REQUIRED: {snapshot.takeover_reason}")
        header.append("端侧已明确请求用户接管；可先观察状态，用户完成后调用 resume_from_user。")
    if snapshot.secure_window:
        header.append("CAPTURE LIMITED: 设备报告 FLAG_SECURE，截图可能受限；这不代表用户已请求接管。")
    lines = ["\n".join(header), "", "UI Elements:"]
    if not snapshot.nodes:
        lines.append(
            "  (no accessibility nodes)" if snapshot.secure_window
            else "  (no accessibility nodes; use include_screenshot=true or OCR)"
        )
    else:
        for node in snapshot.nodes:
            flags: list[str] = []
            if node.clickable:
                flags.append("click")
            if node.editable:
                flags.append("edit")
            if node.scrollable:
                flags.append("scroll")
            if node.focused:
                flags.append("focused")
            if node.checked is not None:
                flags.append("checked" if node.checked else "unchecked")
            flag_str = f" [{','.join(flags)}]" if flags else ""
            label_parts = [part for part in (node.text, node.content_description) if part]
            label = " / ".join(label_parts) if label_parts else "<unlabeled>"
            cx, cy = node.center
            l, t, r, b = node.bounds
            short_cls = node.class_name.rsplit(".", 1)[-1]
            lines.append(f"  [{node.node_id}] {short_cls}: {label!r}{flag_str} bounds=({l},{t},{r},{b}) center=({cx},{cy})")
    return "\n".join(lines)
