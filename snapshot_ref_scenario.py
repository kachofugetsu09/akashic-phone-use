"""真实 Core 工具注册、账本和 UDS 回归；协议 peer 不是 Android 真机。"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

CORE = Path(os.environ["AKASHIC_CORE_ROOT"])
sys.path.insert(0, str(CORE))
os.environ.update(AKASHIC_PLUGIN_DISTRIBUTION="", AKASHIC_EXECUTION_MODE="local")

from agent.plugin_composition import ServiceKey
from agent.plugins.manager import PluginManager
from tests.fixtures.plugin_workspace import initialize_plugin_workspace
from plugins.ledger.contract import BINDINGS, MESSAGE_CATALOG, CallRef, ContentPart, Input, Output, ToolCall
from plugins.tools.contract import ALL_TOOLS, TOOLS, CallSource
from plugins.tools.plugin import open_tool
from core.common.unix_socket import socket_alias_path
from websockets.asyncio.client import unix_connect
from websockets.exceptions import ConnectionClosed

WRITER = '''from agent.plugin_composition import ServiceKey
from plugins.ledger.contract import MESSAGE_WRITERS, BINDINGS, Input, Output
from plugins.tools.contract import TOOLS
from plugins.content.contract import CONTENT
api_version = 3
name = "snapshot_writer"
version = "1.0.0"
inject = (MESSAGE_WRITERS, CONTENT, BINDINGS, TOOLS)
async def apply(ctx):
    def check_call(call):
        metadata = ctx.require(BINDINGS).describe(call.binding_id, TOOLS)
        if metadata["tool"]["name"] not in ("phone_observe", "phone_action"):
            raise PermissionError("snapshot fixture only grants phone tools")
    writer = ctx.require(MESSAGE_WRITERS).bind(ctx, author="snapshot-test", source="snapshot-test",
        body_types=(Input, Output), content={"text":ctx.require(CONTENT).check_text}, check_call=check_call)
    await ctx.provide(ServiceKey("snapshot.writer.v1"), (writer, ctx))
'''


async def main() -> None:
    """在临时 workspace 内实际绑定工具，检查引用通过正式 prepare/invoke 链。"""
    base = Path(tempfile.mkdtemp(prefix="phone-snapshot-ref-"))
    workspace, sources = base / "workspace", base / "sources"
    initialize_plugin_workspace(workspace)
    for name in ("ledger", "content", "tools", "turn_projection", "assets"):
        shutil.copytree(CORE / "plugins" / name, sources / name, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(Path(__file__).parent, sources / "phone_use",
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "tests"))
    (sources / "snapshot_writer").mkdir()
    (sources / "snapshot_writer" / "plugin.py").write_text(WRITER)
    host = PluginManager([sources], workspace=workspace, installed_cache_root=base / "cache")
    ws = None
    peer_task = None
    wire: list[dict] = []
    omit_ref = False
    try:
        # 1. Load the actual Core owners and bind the registered tools.
        await host.load_all()
        root = host.live_root
        assert root is not None
        control = root.context.require(ServiceKey("phone.control.v1"))
        ctx = control.ctx
        writer, writer_ctx = root.context.require(ServiceKey("snapshot.writer.v1"))
        catalog = root.context.require(MESSAGE_CATALOG)
        refs = root.context.require(ALL_TOOLS)()
        action_ref = refs.select("phone_action")
        assert "snapshot_ref" in action_ref.description["parameters"]["properties"]
        assert not {"keep_packages", "close_packages"} & action_ref.description["parameters"]["properties"].keys()
        async with ctx.runtime_scope():
            bindings = ctx.require(BINDINGS)
            tools = ctx.require(TOOLS)
            bound = {name: await tools.bind(refs.select(name), bindings)
                     for name in ("phone_observe", "phone_action")}
        path = socket_alias_path(workspace / "runtime", "phone-use.sock")
        ws = await unix_connect(str(path), uri="ws://localhost/api/phone/ws")
        await ws.send(json.dumps({"type": "hello", "device_id": "snapshot-peer",
            "supports_ack": True, "supports_snapshot_ref": True, "shell_version": "0.5.0"}))
        assert json.loads(await ws.recv())["type"] == "welcome"

        async def peer() -> None:
            """用真实传输返回每个请求自己的引用，不模拟 Android 节点安全检查。"""
            try:
                async for raw in ws:
                    request = json.loads(raw)
                    if request["type"] != "request":
                        continue
                    wire.append(request)
                    await ws.send(json.dumps({"type": "ack", "request_id": request["request_id"]}))
                    screen = {"snapshot_ref": request["request_id"], "display_id": 2,
                        "nodes": [{"node_id": 1, "text": "fixture", "bounds": [0, 0, 20, 20], "editable": True}]}
                    if omit_ref:
                        del screen["snapshot_ref"]
                    result = screen if request["op"] == "observe" else {"message": "fixture received", "screen": screen}
                    await ws.send(json.dumps({"type": "response", "request_id": request["request_id"], "ok": True, "result": result}))
            except ConnectionClosed:
                pass
        peer_task = asyncio.create_task(peer())
        async with writer_ctx.runtime_scope():
            await writer("snapshot-session").append_async("input", Input((ContentPart("text", "snapshot fixture"),)))
        step = 0

        async def call(name: str, arguments: dict) -> object:
            """提交真实 ToolCall 前缀，使用注册表 facade 准备并执行。"""
            nonlocal step
            step += 1
            message_id = f"call-{step}"
            async with writer_ctx.runtime_scope():
                await writer("snapshot-session").append_async(message_id, Output((ToolCall(bound[name], arguments),), "continue"))
            source = CallSource(CallRef(message_id, 0), tuple(catalog.reader("snapshot-session").snapshot()))
            async with ctx.runtime_scope():
                async with open_tool(bindings, bound[name]) as tool:
                    prepared = await tool.prepare(arguments, source)
                    if isinstance(prepared, str):
                        return prepared
                    return await tool.invoke(source.effect_key, prepared)

        # 2. Missing references fail at prepare, including implicit focused edits.
        for args in ({"action": "tap", "node_id": 1}, {"action": "type_text", "text": "x"}, {"action": "clear_text"}):
            refusal = await call("phone_action", {"description": "missing reference", **args})
            assert isinstance(refusal, str) and "snapshot_ref" in refusal
        for selector in ("keep_packages", "close_packages"):
            refusal = await call("phone_action", {"description": "unsupported selection", "action": "tap", "x": 10, "y": 10,
                                                  selector: ["dev.fixture"]})
            assert isinstance(refusal, str) and selector in refusal and "extra_forbidden" in refusal
        assert not wire
        # 3. Returned references are rendered, then sent without rewriting.
        observed = await call("phone_observe", {})
        assert observed.outcome == "success"
        assert "Foreground: unknown/unknown" in observed.parts[0].value
        first_ref = wire[-1]["request_id"]
        assert f"snapshot_ref={first_ref}" in observed.parts[0].value
        edited = await call("phone_action", {"description": "edit fixture", "action": "type_text",
            "node_id": 1, "text": "x", "snapshot_ref": first_ref})
        assert edited.outcome == "success"
        second_ref = wire[-1]["request_id"]
        assert wire[-1]["payload"]["snapshot_ref"] == first_ref and second_ref != first_ref
        assert f"snapshot_ref={second_ref}" in edited.parts[0].value
        tapped = await call("phone_action", {"description": "tap fixture", "action": "tap", "node_id": 1, "snapshot_ref": second_ref})
        assert tapped.outcome == "success" and wire[-1]["payload"]["snapshot_ref"] == second_ref
        # 4. Coordinates do not use cached nodes and need no invented reference.
        coordinate = await call("phone_action", {"description": "coordinate fixture", "action": "tap", "x": 10, "y": 10})
        assert coordinate.outcome == "success" and wire[-1]["payload"].get("snapshot_ref") is None
        # 5. A successful RPC with an undecodable screen has an unknown effect, not a safe retry.
        omit_ref = True
        before = len(wire)
        unknown = await call("phone_action", {"description": "missing response reference", "action": "tap", "x": 10, "y": 10})
        assert unknown.outcome == "error" and "结果不确定，可能已执行" in unknown.parts[0].value
        assert len(wire) == before + 1
        result = {"ok": True, "unsupported_selectors_not_sent": True, "missing_response_ref_unknown_no_retry": True, "missing_ref_not_sent": True, "ref_rendered_and_forwarded": True,
            "coordinate_without_ref": True, "android_owner_checks_tested": False, "wire": wire}
        (base / "result.json").write_text(json.dumps(result, indent=2))
        print("SNAPSHOT_REF_CORE_PASS", base, flush=True)
    finally:
        if ws is not None:
            await ws.close()
        if peer_task is not None:
            await peer_task
        await host.terminate_all()


if __name__ == "__main__":
    asyncio.run(main())
