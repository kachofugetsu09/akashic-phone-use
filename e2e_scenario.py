"""隔离 Core 脚本集成场景（脚本模型与模拟节点，不是真实模型或真机验收）：
1. Prompt Cache 零破坏验证（不调 load_tools 时模型请求 tools schema 不含 phone_*，且手机节点上下线不改变系统提示词与工具目录前缀）；
2. 模拟 Android Shell 节点直接接入 Bridge；
3. Agent 依次执行 load_tools(plugin="phone-use") → phone_devices → phone_action(launch_app) → phone_action(type_text) → phone_action(hand_to_user) → phone_action(resume_from_user) → phone_observe(include_screenshot=True)；
4. Turn 闭合后自动触发 finishDisplay 收尾虚拟屏幕，并将 owner_state 推进至 ended。
"""
from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import cast

PLUGIN_DIR = Path(__file__).resolve().parent
CORE_ROOT = Path("/home/huashen/coding/akashic-agent")
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))

from agent.plugin_composition import ServiceKey
from agent.plugin_composition.config_input import save_config
from agent.plugins.bundles import set_plugin_choice
from agent.plugins.manager import PluginManager
from plugins.channels.contract import CHANNEL_INPUT_V2, ChannelInboundMessage
from plugins.ledger.contract import (
    MESSAGE_CATALOG,
    OWNER_STATE,
    Output,
    ToolResult,
)
from tests.fixtures.plugin_workspace import initialize_plugin_workspace
from tests.support.delivery_sources import sources as delivery_sources

_TINY_PNG_B64 = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
).decode("ascii")


async def run_e2e_scenario(tmp_path: Path) -> dict[str, object]:
    os.environ.update(AKASHIC_PLUGIN_DISTRIBUTION="", AKASHIC_EXECUTION_MODE="local")
    sources = tmp_path / "plugins"
    workspace = tmp_path / "workspace"

    initialize_plugin_workspace(workspace)
    save_config(workspace / "plugin-data/reply-builtin", {"max_output_tokens": 4096})

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

    phone_dst = sources / "phone_use"
    shutil.copytree(
        PLUGIN_DIR,
        phone_dst,
        ignore=shutil.ignore_patterns("__pycache__", ".git", ".pytest_cache", "tests"),
    )

    provider = sources / "test_provider"
    provider.mkdir()
    (provider / "plugin.py").write_text(
        '''from contextlib import asynccontextmanager
from types import SimpleNamespace
import hashlib
import json
from agent.plugin_composition import ServiceKey
from agent.plugin_composition.tasks import TASKS
from plugins.ledger.contract import BINDINGS
from plugins.tools.plugin import TOOLS
from plugins.models.contract import (
    CHAT_MODELS, MODEL_CATALOG, BoundModelDescriptor, CapabilitySources,
    ChatModelSelection, ConnectionDescriptor, LLMResponse, ModelCapabilities,
    ModelCatalogSnapshot, ModelDescriptor, ToolCall,
)
from plugins.models.projection import MODEL_CALLS, MODEL_PROJECTION, ProjectionOwner, MODEL_MESSAGE_CHECKS, MessageChecksOwner
from plugins.models.content import MODEL_CONTENT, ContentOwner
from plugins.models.selection import MODEL_SELECTION, SelectionOwner
from plugins.models.state import _BoundChat, ModelsState
from plugins.models.settings import MODEL_SETTINGS
from plugins.models.store import ModelsStore

api_version = 3
name = "test_provider"
version = "1.0.0"
inject = (TOOLS, TASKS, BINDINGS)

async def apply(ctx):
    executions = []
    store = ModelsStore(ctx.data_root / "models.db", ctx.data_root / "backups")
    store.initialize()
    await ctx.effect(lambda: store.close, label="model-store")
    settings = ModelsState(store, context=ctx)
    await ctx.provide(MODEL_SETTINGS, settings.settings)

    snapshot = ModelCatalogSnapshot(
        revision=1,
        connections=(
            ConnectionDescriptor("conn-local", "Local", "openai-compatible", "api:local", "available", "127.0.0.1", "all"),
        ),
        models=(
            ModelDescriptor("conn-local__default", "conn-local", "chat", "m-default", None,
                            ModelCapabilities(context_window=64000), CapabilitySources(), "available"),
        ),
        role_bindings={"default": "conn-local__default"},
        default_embedding_model_id=None,
    )

    class Catalog:
        def snapshot(self):
            return snapshot
        def validate_chat_selection(self, sel):
            return sel

    await ctx.provide(MODEL_CATALOG, Catalog())
    phone_step = 0

    class Driver:
        max_tool_schemas = None
        def estimate_context_tokens(self, messages, tools):
            return 10
        async def complete(self, request):
            nonlocal phone_step
            tool_names = [t["function"]["name"] for t in (request.tools or ()) if isinstance(t, dict) and "function" in t]
            prefix_digest = hashlib.sha256(
                json.dumps({"system": request.system_prompt, "tools": request.tools}, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            executions.append({"tool_names": tool_names, "prefix_digest": prefix_digest})

            all_text = " ".join(str(m) for m in request.messages)
            if "普通问候" in all_text:
                return LLMResponse("你好，前缀缓存保持稳定。", [])

            phone_step += 1
            if phone_step == 1:
                return LLMResponse(None, [
                    ToolCall("p-load", "load_tools", {"plugin": "phone-use"}),
                ])
            if phone_step == 2:
                return LLMResponse(None, [
                    ToolCall("p-devs", "tool_call", {"name": "phone_devices", "arguments": {}}),
                ])
            if phone_step == 3:
                return LLMResponse(None, [
                    ToolCall("p-launch", "tool_call", {
                        "name": "phone_action",
                        "arguments": {
                            "description": "在后台虚拟屏启动微信",
                            "action": "launch_app",
                            "package_name": "com.tencent.mm",
                        },
                    }),
                ])
            if phone_step == 4:
                return LLMResponse(None, [
                    ToolCall("p-type", "tool_call", {
                        "name": "phone_action",
                        "arguments": {
                            "description": "输入群聊搜索词",
                            "action": "type_text",
                            "node_id": 1,
                            "text": "Akashic 研发群",
                        },
                    }),
                ])
            if phone_step == 5:
                return LLMResponse(None, [
                    ToolCall("p-handoff", "tool_call", {
                        "name": "phone_action",
                        "arguments": {
                            "description": "按用户要求移交主屏检查选项",
                            "action": "hand_to_user",
                            "reason": "请用户检查当前选项",
                        },
                    }),
                ])
            if phone_step == 6:
                return LLMResponse(None, [
                    ToolCall("p-resume", "tool_call", {
                        "name": "phone_action",
                        "arguments": {
                            "description": "用户已在手机主屏幕完成确认，收回后台虚拟屏",
                            "action": "resume_from_user",
                        },
                    }),
                ])
            if phone_step == 7:
                return LLMResponse(None, [
                    ToolCall("p-obs", "tool_call", {
                        "name": "phone_observe",
                        "arguments": {
                            "include_screenshot": True,
                        },
                    }),
                ])
            return LLMResponse("已在手机后台虚拟屏完成微信操作并截图核对。", [])

    def make_bound():
        mid = "conn-local__default"
        descriptor = BoundModelDescriptor(
            binding_id="bind:default", plugin_snapshot_id="fixture", model_revision=1,
            model_id=mid, connection_id="conn-local", driver_id="openai-compatible",
            driver_contract_version="1", auth_identity="fixture", model=mid, role="chat",
            reasoning_effort=None, capabilities=ModelCapabilities(context_window=64000),
            capability_sources=CapabilitySources(), capability_digest="fixture",
        )
        return _BoundChat(descriptor, Driver(), store)

    class Models:
        async def resolve(self, use, choice, session=ChatModelSelection()):
            return session
        @asynccontextmanager
        async def execution(self, *, model_id=None, reasoning_effort=None):
            bound = make_bound()
            async def use(u, c):
                return bound
            async with ctx.runtime_scope():
                yield SimpleNamespace(chat=lambda: bound, use=use)

    await ctx.provide(CHAT_MODELS, Models())
    await ctx.provide(MODEL_CALLS, store.read_call)
    await ctx.provide(MODEL_PROJECTION, ProjectionOwner())
    await ctx.provide(MODEL_MESSAGE_CHECKS, MessageChecksOwner())
    await ctx.provide(MODEL_CONTENT, ContentOwner())
    await ctx.provide(MODEL_SELECTION, SelectionOwner())
    await ctx.provide(ServiceKey("fixture.executions"), executions)
''',
        encoding="utf-8",
    )

    set_plugin_choice(workspace, "models", enabled=False)
    host = PluginManager([sources], workspace=workspace, installed_cache_root=tmp_path / "home/cache")
    await host.load_all()
    try:
        root = host.live_root
        assert root is not None
        ingress = root.context.require(CHANNEL_INPUT_V2)
        catalog = root.context.require(MESSAGE_CATALOG)
        executions = cast(
            list[dict[str, object]],
            root.context.require(ServiceKey("fixture.executions")),
        )
        from plugins.phone_use.contract import PHONE_BRIDGE
        bridge = root.context.require(PHONE_BRIDGE)

        async def wait_complete(session_id: str) -> None:
            reader = catalog.reader(session_id)
            async with asyncio.timeout(20):
                while True:
                    msgs = reader.snapshot()
                    if msgs and isinstance(msgs[-1].body, Output) and msgs[-1].body.finish == "complete":
                        return
                    await asyncio.sleep(0.05)

        # 1. 设备未上线时发一条普通问候
        await ingress(
            "akashic:cache-1",
            "in-cache-1",
            ChannelInboundMessage("test", "user", "room-1", "普通问候 1", datetime.now(UTC), {}),
        )
        await wait_complete("akashic:cache-1")
        digest_before = executions[-1]["prefix_digest"]
        initial_tools = cast(list[str], executions[-1]["tool_names"])
        assert "phone_devices" not in initial_tools
        assert "phone_observe" not in initial_tools
        assert "phone_action" not in initial_tools

        # 2. 注册模拟的 Pixel 9 无线调试虚拟屏节点
        node_requests: list[dict[str, object]] = []
        takeover_stage = False

        async def on_node_request(msg: dict[str, object]) -> None:
            nonlocal takeover_stage
            if msg.get("type") != "request":
                return
            node_requests.append(msg)
            req_id = str(msg["request_id"])
            op = str(msg["op"])
            payload = cast(dict[str, object], msg.get("payload", {}))
            action = str(payload.get("action") or "")

            if op == "finish":
                bridge.handle_incoming(
                    node_session,
                    {
                        "type": "response",
                        "request_id": req_id,
                        "ok": True,
                        "result": {"kept": payload.get("keep", []), "closed": payload.get("close", [])},
                        "state": {"virtual_display_id": None, "pip_state": "done"},
                    },
                )
                return

            if action == "hand_to_user":
                takeover_stage = True
            elif action == "resume_from_user":
                takeover_stage = False

            screen: dict[str, object] = {
                "display_id": 2,
                "width": 1080,
                "height": 2400,
                "rotation": 0,
                "package_name": "com.tencent.mm",
                "activity_name": "com.tencent.mm.ui.LauncherUI",
                "pip_state": "needs_you" if takeover_stage else "running",
                "takeover_reason": "请用户检查当前选项" if takeover_stage else None,
                "audio_muted": True,
                "nodes": [
                    {
                        "node_id": 1,
                        "class_name": "android.widget.EditText",
                        "text": str(payload.get("text") or "搜索"),
                        "content_description": "搜索框",
                        "bounds": [40, 120, 1040, 220],
                        "clickable": True,
                        "editable": True,
                    },
                    {
                        "node_id": 2,
                        "class_name": "android.widget.Button",
                        "text": "检查选项" if takeover_stage else "进入群聊",
                        "content_description": "",
                        "bounds": [700, 2000, 1020, 2140],
                        "clickable": True,
                    },
                ],
            }
            if payload.get("include_screenshot"):
                screen["screenshot_b64"] = _TINY_PNG_B64
            bridge.handle_incoming(
                node_session,
                {
                    "type": "response",
                    "request_id": req_id,
                    "ok": True,
                    "result": (
                        {"message": f"已完成 {action}", "screen": screen}
                        if op == "action"
                        else screen
                    ),
                },
            )

        async def noop_close() -> None:
            pass

        node_session = await bridge.register_node(
            {
                "type": "hello",
                "device_id": "pixel-9",
                "device_name": "花月的 Pixel 9",
                "model": "Pixel 9",
                "sdk_int": 35,
                "shell_version": "0.3.0",
                "display_mode": "virtual_display",
                "virtual_display_id": 2,
                "own_focus": True,
                "steal_top_focus_disabled": True,
                "pip_state": "running",
                "active_packages": ["com.tencent.mm"],
            },
            send_json=on_node_request,
            close_transport=noop_close,
        )

        # 3. 手机上线后再发一条普通问候：验证 Prompt Cache 前缀哈希 100% 相同
        await ingress(
            "akashic:cache-2",
            "in-cache-2",
            ChannelInboundMessage("test", "user", "room-2", "普通问候 2", datetime.now(UTC), {}),
        )
        await wait_complete("akashic:cache-2")
        digest_after = executions[-1]["prefix_digest"]
        assert digest_before == digest_after, "设备上下线不得破坏 Prompt Cache 前缀！"

        # 4. 触发完整 Phone Use 全链路会话
        await ingress(
            "akashic:phone-e2e",
            "in-phone-e2e",
            ChannelInboundMessage("test", "user", "room-3", "帮我在手机后台打开微信群处理一下并截图", datetime.now(UTC), {}),
        )
        await wait_complete("akashic:phone-e2e")

        # 5. 等待 Turn 闭合后后台 follower 自动发送 finishDisplay 并把 owner_state 标为 ended
        phone_fiber = next(f for f in root.fibers() if f.name == "phone-use")
        async with phone_fiber.context.runtime_scope():
            owner_store = phone_fiber.context.require(OWNER_STATE).open(phone_fiber.context)
        ended_records = []
        async with asyncio.timeout(10):
            while True:
                items = [
                    (k, r.value)
                    for k, r in owner_store.list()
                    if k.startswith("phone-use:")
                ]
                if items and all(v.get("phase") == "ended" for _, v in items):
                    ended_records = items
                    break
                await asyncio.sleep(0.05)

        snap_msgs = catalog.reader("akashic:phone-e2e").snapshot()
        tool_results = [m.body for m in snap_msgs if isinstance(m.body, ToolResult)]
        assert len(tool_results) == 7
        assert all(tr.outcome == "success" for tr in tool_results)
        last_parts = tool_results[-1].parts
        assert any(p.kind == "artifact_ref" for p in last_parts)
        ops = [str(r["op"]) for r in node_requests]
        assert "finish" in ops, f"未收到 Turn 闭合自动 finish 指令: {ops}"

        return {
            "prompt_cache_preserved": digest_before == digest_after,
            "prefix_digest": digest_before,
            "tool_steps_completed": len(tool_results),
            "node_rpc_ops": ops,
            "ended_owner_records": len(ended_records),
        }
    finally:
        if host.live_root is not None:
            await host.live_root.dispose()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="phone-use-e2e-") as tmp:
        report = asyncio.run(run_e2e_scenario(Path(tmp)))
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
