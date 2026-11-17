"""验证 phone-use 在 Control(failure/pause/resume) 与断线重连场景下的虚拟屏收尾生命周期。"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
import shutil
import sys
from typing import cast

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
CORE_ROOT = Path("/home/huashen/coding/akashic-agent")
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))

from agent.plugins.manager import PluginManager
from plugins.content.contract import CONTENT
from plugins.ledger.contract import (
    MESSAGE_CATALOG,
    MESSAGE_WRITERS,
    OWNER_STATE,
    ContentPart,
    Control,
    Input,
    Output,
)
from tests.fixtures.plugin_workspace import initialize_plugin_workspace
from tests.support.delivery_sources import sources as delivery_sources


async def _mount_phone_use(tmp_path: Path):
    os.environ.update(AKASHIC_PLUGIN_DISTRIBUTION="", AKASHIC_EXECUTION_MODE="local")
    sources = tmp_path / "plugins"
    workspace = tmp_path / "workspace"
    initialize_plugin_workspace(workspace)

    for name in (
        "ledger",
        "commands",
        "sources",
        "content",
        "context",
        "tools",
        "conversation",
        "react",
        "turn_projection",
        "reply_program",
        "reply",
        "working_directory",
        "shell",
        "files",
        "tool_search",
        "jobs",
        "host_execution",
        "assets",
    ):
        shutil.copytree(
            CORE_ROOT / "plugins" / name,
            sources / name,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    delivery_sources(sources)
    shutil.copytree(
        PLUGIN_DIR,
        sources / "phone_use",
        ignore=shutil.ignore_patterns("__pycache__", ".git", ".pytest_cache", "tests"),
    )
    helper = sources / "test_writer"
    helper.mkdir()
    (helper / "plugin.py").write_text(
        '''from agent.plugin_composition import ServiceKey
from plugins.content.contract import CONTENT
from plugins.ledger.contract import MESSAGE_WRITERS, Control, Input, Output

api_version = 3
name = "test_writer"
version = "1.0.0"
inject = (MESSAGE_WRITERS, CONTENT)

async def apply(ctx):
    writers = ctx.require(MESSAGE_WRITERS)
    text_check = {"text": ctx.require(CONTENT).check_text}
    prog_in = writers.bind(ctx, author="app", source="programmatic", body_types=(Input,), content=text_check)
    prog_ctrl = writers.bind(ctx, author="app", source="programmatic", body_types=(Control,), content=text_check)
    conv_in = writers.bind(ctx, author="app", source="conversation", body_types=(Input,), content=text_check)
    conv_ctrl = writers.bind(ctx, author="app", source="conversation", body_types=(Control,), content=text_check)
    conv_out = writers.bind(ctx, author="assistant", source="conversation", body_types=(Output,), content=text_check)
    async def append_msg(session_id: str, source: str, kind: str, msg_id: str, body):
        factory = {
            ("programmatic", "in"): prog_in,
            ("programmatic", "ctrl"): prog_ctrl,
            ("conversation", "in"): conv_in,
            ("conversation", "ctrl"): conv_ctrl,
            ("conversation", "out"): conv_out,
        }[(source, kind)]
        writer = factory(session_id)
        try:
            return writer.append(msg_id, body)
        finally:
            writer.expire()
    await ctx.provide(ServiceKey("fixture.append_msg"), ctx.entrypoint(append_msg))
''',
        encoding="utf-8",
    )

    host = PluginManager([sources], workspace=workspace, installed_cache_root=tmp_path / "home/cache")
    await host.load_all()
    assert host.live_root is not None
    return host


@pytest.mark.asyncio
async def test_failure_and_pause_and_reconnect_cleanup(tmp_path: Path) -> None:
    host = await _mount_phone_use(tmp_path)
    try:
        root = host.live_root
        assert root is not None
        from plugins.phone_use.contract import PHONE_BRIDGE

        bridge = root.context.require(PHONE_BRIDGE)
        phone_fiber = next(f for f in root.fibers() if f.name == "phone-use")
        async with phone_fiber.context.runtime_scope():
            owner_store = phone_fiber.context.require(OWNER_STATE).open(phone_fiber.context)
            catalog = root.context.require(MESSAGE_CATALOG)
            writers = root.context.require(MESSAGE_WRITERS)

        # 模拟手机上线（初始无虚拟屏）
        finish_reasons: list[str] = []
        vd_state = {"display_id": None, "pip_state": "hidden"}
        node_session = None

        async def connect_node(*, display_id: int | None, pip_state: str):
            nonlocal node_session
            vd_state["display_id"] = display_id
            vd_state["pip_state"] = pip_state

            async def send_json(msg: dict[str, object]) -> None:
                if msg.get("type") != "request":
                    return
                req_id = str(msg["request_id"])
                op = str(msg["op"])
                payload = cast(dict[str, object], msg.get("payload", {}))
                if op == "finish":
                    finish_reasons.append(str(payload.get("reason") or ""))
                    vd_state["display_id"] = None
                    vd_state["pip_state"] = "done"
                    assert node_session is not None
                    bridge.handle_incoming(
                        node_session,
                        {
                            "type": "response",
                            "request_id": req_id,
                            "ok": True,
                            "result": {"kept": [], "closed": []},
                            "state": {"virtual_display_id": None, "pip_state": "done"},
                        },
                    )
                    return
                vd_state["display_id"] = 12
                vd_state["pip_state"] = "running"
                assert node_session is not None
                bridge.handle_incoming(
                    node_session,
                    {
                        "type": "response",
                        "request_id": req_id,
                        "ok": True,
                        "result": {
                            "snapshot_ref": req_id,
                            "display_id": 12,
                            "width": 1080,
                            "height": 2400,
                            "rotation": 0,
                            "package_name": "com.android.settings",
                            "activity_name": "Wellbeing",
                            "pip_state": "running",
                            "audio_muted": True,
                            "nodes": [],
                        },
                    },
                )

            async def noop_close() -> None:
                pass

            node_session = await bridge.register_node(
                {
                    "type": "hello",
                    "supports_snapshot_ref": True,
                    "device_id": "pixel-9",
                    "device_name": "Pixel 9",
                    "model": "Pixel 9",
                    "sdk_int": 36,
                    "shell_version": "0.5.1",
                    "display_mode": "virtual_display",
                    "virtual_display_id": display_id,
                    "own_focus": True,
                    "steal_top_focus_disabled": True,
                    "pip_state": pip_state,
                    "active_packages": ["com.android.settings"] if display_id else [],
                },
                send_json=send_json,
                close_transport=noop_close,
            )
            return node_session

        await connect_node(display_id=None, pip_state="hidden")

        # 1. 构造 programmatic:watch 失败终态场景：Input -> Output(continue) -> Control(failure)
        from plugins.tools.contract import TOOLS
        tools = root.context.require(TOOLS)
        async with phone_fiber.context.runtime_scope():
            from plugins.phone_use.plugin import PHONE_CONTROL, PhoneCallIdentity, PhoneControl
            control = cast(PhoneControl, phone_fiber.context.require(PHONE_CONTROL))
            # 通过绑定记录一次 started
            from plugins.ledger.contract import BINDINGS
            binding_id = phone_fiber.context.require(BINDINGS).bind(
                PHONE_CONTROL, {"version": 1}, contributors=(phone_fiber.context,)
            )

        sid_fail = "programmatic:watch-fail-test"
        sid_pause = "akashic:pause-test"
        from agent.plugin_composition import ServiceKey
        append_msg = root.context.require(ServiceKey("fixture.append_msg"))
        await append_msg(sid_fail, "programmatic", "in", "in-fail-1", Input((ContentPart("text", "check phone"),)))

        async with phone_fiber.context.runtime_scope():
            await control.observe(
                '["msg-fail-1",0]',
                binding_id,
                {"_phone": {"session_id": sid_fail, "source": "programmatic", "turn_input_id": "in-fail-1"}},
            )
        assert vd_state["display_id"] == 12
        assert vd_state["pip_state"] == "running"

        # 写入 Control(action="failure")，不经过 Output(complete) 或 Control(abandon)
        await append_msg(sid_fail, "programmatic", "ctrl", "ctrl-fail-1", Control("failure", 0, "pointer 当前 activation 不接纳新调用"))

        async with asyncio.timeout(5):
            while True:
                async with phone_fiber.context.runtime_scope():
                    rec = owner_store.read('phone-use:["msg-fail-1",0]')
                if rec is not None and rec.value.get("phase") == "ended":
                    break
                await asyncio.sleep(0.05)

        assert "turn_ended" in finish_reasons
        assert vd_state["display_id"] is None

        # 2. 构造 pause -> resume (不应结束) -> pause (应结束)
        await append_msg(sid_pause, "conversation", "in", "in-pause-1", Input((ContentPart("text", "open settings"),)))

        async with phone_fiber.context.runtime_scope():
            await control.observe(
                '["msg-pause-1",0]',
                binding_id,
                {"_phone": {"session_id": sid_pause, "source": "conversation", "turn_input_id": "in-pause-1"}},
            )
        before_pause_count = len(finish_reasons)
        await append_msg(sid_pause, "conversation", "ctrl", "ctrl-pause-1", Control("pause", 0, "user stop"))
        async with asyncio.timeout(5):
            while True:
                async with phone_fiber.context.runtime_scope():
                    rec = owner_store.read('phone-use:["msg-pause-1",0]')
                if rec is not None and rec.value.get("phase") == "ended":
                    break
                await asyncio.sleep(0.05)
        assert len(finish_reasons) == before_pause_count + 1
        assert vd_state["display_id"] is None

        # 3. 构造 pause 后紧跟 resume（此时仍未结束），最后 complete 才结束
        await append_msg(sid_pause, "conversation", "in", "in-resume-1", Input((ContentPart("text", "open settings 2"),)))
        async with phone_fiber.context.runtime_scope():
            await control.observe(
                '["msg-resume-1",0]',
                binding_id,
                {"_phone": {"session_id": sid_pause, "source": "conversation", "turn_input_id": "in-resume-1"}},
            )
        await append_msg(sid_pause, "conversation", "ctrl", "ctrl-pause-2", Control("pause", 2, "pause"))
        await append_msg(sid_pause, "conversation", "ctrl", "ctrl-resume-2", Control("resume", 3))
        await asyncio.sleep(0.2)
        async with phone_fiber.context.runtime_scope():
            rec_resumed = owner_store.read('phone-use:["msg-resume-1",0]')
        assert rec_resumed is not None and rec_resumed.value.get("phase") == "started"
        await append_msg(sid_pause, "conversation", "out", "out-done-2", Output((ContentPart("text", "done"),), "complete"))
        async with asyncio.timeout(5):
            while True:
                async with phone_fiber.context.runtime_scope():
                    rec = owner_store.read('phone-use:["msg-resume-1",0]')
                if rec is not None and rec.value.get("phase") == "ended":
                    break
                await asyncio.sleep(0.05)
        assert vd_state["display_id"] is None

        # 4. 构造断线重连场景：手机带残余 virtual_display_id=12 & pip_state="running" 上线且无活跃 turn，应自动清理
        await bridge.unregister_node(node_session, "offline")
        before_reconcile = len(finish_reasons)
        await connect_node(display_id=12, pip_state="running")
        async with asyncio.timeout(5):
            while len(finish_reasons) == before_reconcile:
                await asyncio.sleep(0.05)
        assert finish_reasons[-1] == "orphaned_display_cleanup"
        assert vd_state["display_id"] is None
    finally:
        if host.live_root is not None:
            await host.live_root.dispose()
