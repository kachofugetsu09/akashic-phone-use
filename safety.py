"""端侧安全接管（Ask / Takeover）判定与 UI 节点树格式化。"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from .contract import PhoneScreenSnapshot, PhoneUiNode

# 支付收银台、指纹/密码认证、银行安全键盘与验证码页面的确定性识别规则。
_SENSITIVE_ACTIVITY_KEYWORDS = (
    "pay",
    "cashier",
    "checkout",
    "wallet",
    "biometric",
    "keyguard",
    "auth",
    "verify",
    "passcode",
    "password",
    "transfer",
)

_SENSITIVE_TEXT_KEYWORDS = (
    "确认支付",
    "立即支付",
    "输入支付密码",
    "支付密码",
    "指纹支付",
    "面容支付",
    "确认转账",
    "立即付款",
    "安全验证",
    "短信验证码",
    "请输入验证码",
    "滑块验证",
    "手势密码",
)


def detect_takeover_reason(
    *,
    package_name: str,
    activity_name: str,
    nodes: Sequence[PhoneUiNode],
    explicit_reason: str | None = None,
    secure_window: bool = False,
) -> str | None:
    """识别需要把后台虚拟屏 App 移交主屏由用户本人完成的敏感场景。"""
    if explicit_reason:
        return explicit_reason
    if secure_window:
        return f"应用窗口标记了 FLAG_SECURE ({package_name}/{activity_name})，需用户在主屏幕接管操作"
    lowered_activity = f"{package_name}/{activity_name}".lower()
    for token in _SENSITIVE_ACTIVITY_KEYWORDS:
        if token in lowered_activity:
            for node in nodes:
                combined = f"{node.text} {node.content_description}".strip()
                for phrase in _SENSITIVE_TEXT_KEYWORDS:
                    if phrase in combined:
                        return f"检测到敏感页面【{phrase}】({package_name})，已挂起后台操作并请求主屏接管"
    for node in nodes:
        combined = f"{node.text} {node.content_description}".strip()
        for phrase in ("输入支付密码", "确认支付", "立即支付", "确认转账", "指纹支付", "面容支付"):
            if phrase in combined:
                return f"检测到支付/转账确认控件【{phrase}】({package_name})，需用户在主屏幕亲自确认"
    return None


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
        f"Device: {snapshot.device_id} | VirtualDisplay: #{snapshot.display_id} ({snapshot.width}x{snapshot.height} rot={snapshot.rotation})",
        f"Foreground: {snapshot.package_name}/{snapshot.activity_name} | PiP: {snapshot.pip_state} | AudioMuted: {snapshot.audio_muted}",
    ]
    if snapshot.takeover_reason:
        header.append(f"TAKEOVER REQUIRED: {snapshot.takeover_reason}")
        header.append("请告知用户在手机主屏或点击右下角橙色呼吸小窗完成操作，完成后再调用 resume_from_user 或 phone_observe。")
    lines = ["\n".join(header), "", "UI Elements:"]
    if not snapshot.nodes:
        lines.append("  (no accessibility nodes; use include_screenshot=true or OCR)")
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
