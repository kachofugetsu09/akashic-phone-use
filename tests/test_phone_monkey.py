"""Phone Bridge 状态机、多设备路由、主屏接管护栏与并发断连的确定性 Monkey 测试（200 seeds）。"""
from __future__ import annotations

import asyncio
import base64
import random
from typing import cast

import pytest

import importlib.util
from pathlib import Path
import sys

def _load_package():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "phone_use_pkg",
        root / "contract.py",
        submodule_search_locations=[str(root)],
    )
    assert spec is not None and spec.loader is not None
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["phone_use_pkg"] = pkg
    spec.loader.exec_module(pkg)
    for mod in ("contract", "safety", "bridge"):
        m_spec = importlib.util.spec_from_file_location(
            f"phone_use_pkg.{mod}",
            root / f"{mod}.py",
        )
        assert m_spec is not None and m_spec.loader is not None
        module = importlib.util.module_from_spec(m_spec)
        sys.modules[f"phone_use_pkg.{mod}"] = module
        m_spec.loader.exec_module(module)

_load_package()
from phone_use_pkg.bridge import PhoneBridge, PhoneNodeError
from phone_use_pkg.safety import detect_takeover_reason, format_screen_snapshot, parse_ui_nodes

_TINY_PNG = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
).decode("ascii")


class _SimulatedAndroidNode:
    def __init__(self, device_id: str, bridge: PhoneBridge, rng: random.Random) -> None:
        self.device_id = device_id
        self.bridge = bridge
        self.rng = rng
        self.session = None
        self.display_id = rng.choice([None, 2, 3])
        self.pip_state = "hidden" if self.display_id is None else "running"
        self.active_packages: list[str] = []
        self.takeover_reason: str | None = None
        self.closed = False
        self.drop_next = False
        self.fail_next = False
        self.payment_page = False

    async def connect(self) -> None:
        self.session = await self.bridge.register_node(
            {
                "type": "hello",
                "device_id": self.device_id,
                "device_name": f"Phone-{self.device_id}",
                "model": "Pixel 9",
                "sdk_int": 35,
                "shell_version": "0.3.0",
                "display_mode": "virtual_display",
                "virtual_display_id": self.display_id,
                "own_focus": True,
                "steal_top_focus_disabled": True,
                "pip_state": self.pip_state,
                "active_packages": list(self.active_packages),
                "takeover_reason": self.takeover_reason,
            },
            send_json=self._on_server_message,
            close_transport=self._on_close,
        )
        self.closed = False

    async def _on_close(self) -> None:
        self.closed = True

    async def _on_server_message(self, message: dict[str, object]) -> None:
        if message.get("type") != "request":
            return
        req_id = str(message["request_id"])
        op = str(message["op"])
        payload = cast(dict[str, object], message.get("payload", {}))

        if self.drop_next:
            self.drop_next = False
            assert self.session is not None
            await self.bridge.unregister_node(self.session, "monkey drop")
            return

        if self.fail_next:
            self.fail_next = False
            assert self.session is not None
            self.bridge.handle_incoming(
                self.session,
                {"type": "response", "request_id": req_id, "ok": False, "error": "START_IN_USE_ON_MAIN"},
            )
            return

        if self.display_id is None:
            self.display_id = 2
            self.pip_state = "running"

        if op == "finish":
            keep = [str(p) for p in cast(list[object], payload.get("keep", []))]
            close = [str(p) for p in cast(list[object], payload.get("close", []))]
            self.active_packages = [p for p in self.active_packages if p in keep and p not in close]
            self.display_id = None
            self.pip_state = "done"
            self.takeover_reason = None
            self.payment_page = False
            assert self.session is not None
            self.bridge.handle_incoming(
                self.session,
                {
                    "type": "response",
                    "request_id": req_id,
                    "ok": True,
                    "result": {"kept": keep, "closed": close},
                    "state": {
                        "virtual_display_id": None,
                        "pip_state": "done",
                        "active_packages": list(self.active_packages),
                        "takeover_reason": None,
                    },
                },
            )
            return

        action = str(payload.get("action") or "")
        if action == "launch_app":
            pkg = str(payload.get("package_name") or "com.tencent.mm")
            if pkg not in self.active_packages:
                self.active_packages.append(pkg)
        elif action == "hand_to_user":
            self.takeover_reason = str(payload.get("reason") or "需要用户确认支付")
            self.pip_state = "needs_you"
        elif action == "resume_from_user":
            self.takeover_reason = None
            self.payment_page = False
            self.pip_state = "running"

        pkg = self.active_packages[-1] if self.active_packages else "com.tencent.mm"
        act = "com.tencent.mm.plugin.wallet.PayActivity" if self.payment_page else "com.tencent.mm.ui.LauncherUI"
        if self.payment_page and not self.takeover_reason:
            self.takeover_reason = f"检测到敏感页面【立即支付】({pkg})，已挂起后台操作并请求主屏接管"
            self.pip_state = "needs_you"
        nodes = [
            {
                "node_id": 1,
                "class_name": "android.widget.EditText",
                "text": "搜索",
                "content_description": "搜索输入框",
                "bounds": [40, 120, 1040, 220],
                "clickable": True,
                "editable": True,
            },
            {
                "node_id": 2,
                "class_name": "android.widget.Button",
                "text": "立即支付" if self.payment_page else "发送",
                "content_description": "",
                "bounds": [800, 2100, 1020, 2240],
                "clickable": True,
            },
        ]
        screen: dict[str, object] = {
            "display_id": self.display_id,
            "width": 1080,
            "height": 2400,
            "rotation": 0,
            "package_name": pkg,
            "activity_name": act,
            "pip_state": self.pip_state,
            "audio_muted": True,
            "nodes": nodes,
            "takeover_reason": self.takeover_reason,
        }
        if payload.get("include_screenshot"):
            screen["screenshot_b64"] = _TINY_PNG
        assert self.session is not None
        self.bridge.handle_incoming(
            self.session,
            {
                "type": "response",
                "request_id": req_id,
                "ok": True,
                "result": {"message": f"executed {op}:{action}", "screen": screen} if op == "action" else screen,
                "state": {
                    "virtual_display_id": self.display_id,
                    "pip_state": self.pip_state,
                    "active_packages": list(self.active_packages),
                },
            },
        )


def _assert_invariants(bridge: PhoneBridge, nodes: dict[str, _SimulatedAndroidNode]) -> None:
    descriptors = bridge.list_devices()
    ids = [d.device_id for d in descriptors]
    # 1. 设备列表按 device_id 严格递增且无重复
    assert ids == sorted(set(ids))
    # 2. 所有在册设备必须是未关闭的活跃会话
    for d in descriptors:
        sim = nodes[d.device_id]
        assert not sim.closed
        if d.takeover_reason is not None:
            assert d.pip_state == "needs_you"


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(200))
async def test_monkey_phone_bridge_invariants(seed: int) -> None:
    rng = random.Random(seed)
    bridge = PhoneBridge()
    pool = {
        name: _SimulatedAndroidNode(name, bridge, rng)
        for name in ("pixel-9", "tablet-pro")
    }

    for _ in range(25):
        _assert_invariants(bridge, pool)
        op = rng.choice(
            [
                "connect",
                "disconnect",
                "reconnect",
                "observe",
                "launch_app",
                "tap",
                "trigger_payment_takeover",
                "hand_to_user",
                "resume_from_user",
                "finish",
                "drop_during_rpc",
                "main_screen_conflict",
            ]
        )
        dev_id = rng.choice(["pixel-9", "tablet-pro"])
        sim = pool[dev_id]
        online_ids = [d.device_id for d in bridge.list_devices()]
        arg_device = None if len(online_ids) == 1 and online_ids[0] == dev_id and rng.random() < 0.5 else dev_id

        if op == "connect":
            if sim.closed or sim.session is None or dev_id not in online_ids:
                await sim.connect()
        elif op == "disconnect":
            if sim.session is not None and dev_id in online_ids:
                await bridge.unregister_node(sim.session, "user closed")
        elif op == "reconnect":
            await sim.connect()
        elif op == "observe":
            if dev_id not in online_ids:
                with pytest.raises(PhoneNodeError):
                    await bridge.observe(arg_device)
            else:
                want_img = rng.random() < 0.3
                snap = await bridge.observe(arg_device, include_screenshot=want_img)
                assert snap.device_id == dev_id
                assert (snap.screenshot_png is not None) == want_img
                rendered = format_screen_snapshot(snap)
                assert dev_id in rendered
        elif op == "launch_app":
            if dev_id not in online_ids or sim.takeover_reason:
                with pytest.raises(PhoneNodeError):
                    await bridge.perform_action(
                        arg_device,
                        {"action": "launch_app", "package_name": "com.xingin.xhs"},
                    )
            else:
                _, snap = await bridge.perform_action(
                    arg_device,
                    {"action": "launch_app", "package_name": "com.xingin.xhs"},
                )
                assert snap is not None and snap.package_name == "com.xingin.xhs"
        elif op == "tap":
            if dev_id not in online_ids or sim.takeover_reason:
                with pytest.raises(PhoneNodeError):
                    await bridge.perform_action(arg_device, {"action": "tap", "node_id": 1})
            else:
                _, snap = await bridge.perform_action(arg_device, {"action": "tap", "node_id": 1})
                assert snap is not None
        elif op == "trigger_payment_takeover":
            if dev_id in online_ids:
                sim.payment_page = True
                snap = await bridge.observe(dev_id)
                assert snap.takeover_reason is not None
                assert snap.pip_state == "needs_you"
                # 进入接管态后，普通点击必须被拦截
                with pytest.raises(PhoneNodeError):
                    await bridge.perform_action(dev_id, {"action": "tap", "node_id": 2})
        elif op == "hand_to_user":
            if dev_id in online_ids:
                _, snap = await bridge.perform_action(
                    dev_id,
                    {"action": "hand_to_user", "reason": "请输入支付密码"},
                )
                assert snap is not None
                assert snap.takeover_reason == "请输入支付密码"
                assert snap.pip_state == "needs_you"
        elif op == "resume_from_user":
            if dev_id in online_ids:
                _, snap = await bridge.perform_action(dev_id, {"action": "resume_from_user"})
                assert snap is not None
                assert snap.takeover_reason is None
                assert snap.pip_state == "running"
        elif op == "finish":
            if dev_id in online_ids:
                res = await bridge.finish_display(dev_id, keep_packages=("com.tencent.mm",))
                assert "kept" in res
        elif op == "drop_during_rpc":
            if dev_id in online_ids:
                sim.drop_next = True
                with pytest.raises(PhoneNodeError):
                    await bridge.observe(dev_id, timeout_ms=500)
                assert dev_id not in [d.device_id for d in bridge.list_devices()]
        elif op == "main_screen_conflict":
            if dev_id in online_ids and not sim.takeover_reason:
                sim.fail_next = True
                with pytest.raises(PhoneNodeError, match="START_IN_USE_ON_MAIN"):
                    await bridge.perform_action(
                        dev_id,
                        {"action": "launch_app", "package_name": "com.tencent.mm"},
                    )

    await bridge.close_all()
    assert bridge.list_devices() == ()
