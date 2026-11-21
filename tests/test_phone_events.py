"""验证端侧通用事件总线：订阅规则持久/推送、事件幂等入库、Watch 与 UserInformation 适配。"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import time

from fastapi.testclient import TestClient
import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
CORE_ROOT = Path("/home/huashen/coding/akashic-agent")
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))


def _load_pkg():
    spec = importlib.util.spec_from_file_location(
        "phone_use_events_test_pkg",
        PLUGIN_DIR / "contract.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["phone_use_events_test_pkg"] = pkg
    spec.loader.exec_module(pkg)
    for mod in ("contract", "safety", "events", "bridge", "server", "tools"):
        m_spec = importlib.util.spec_from_file_location(
            f"phone_use_events_test_pkg.{mod}",
            PLUGIN_DIR / f"{mod}.py",
        )
        assert m_spec is not None and m_spec.loader is not None
        module = importlib.util.module_from_spec(m_spec)
        sys.modules[f"phone_use_events_test_pkg.{mod}"] = module
        m_spec.loader.exec_module(module)
    return pkg


_load_pkg()
from phone_use_events_test_pkg.bridge import PhoneBridge  # type: ignore[import-not-found]
from phone_use_events_test_pkg.events import (  # type: ignore[import-not-found]
    PhoneEventStore,
    PhoneInformationProvider,
    PhoneSubscriptionRule,
    RuleActionsConfig,
    RuleMatchConfig,
)
from phone_use_events_test_pkg.server import create_phone_app  # type: ignore[import-not-found]
from phone_use_events_test_pkg.tools import DeviceSubscribeTool, PhoneEventsTool  # type: ignore[import-not-found]


def test_store_rules_lifecycle_and_ttl(tmp_path: Path) -> None:
    store = PhoneEventStore(tmp_path)
    initial = store.list_rules()
    assert initial["rules_version"] == 1
    ids = {r["id"] for r in initial["rules"]}
    assert {"wechat-notifications", "app-sessions", "appops-sensors", "media-playback", "device-context"} <= ids

    custom = PhoneSubscriptionRule(
        id="meituan-order",
        adapter="notification",
        package="com.sankuai.meituan",
        match=RuleMatchConfig(title_regex="骑手|送达"),
        actions=RuleActionsConfig(auto_cancel=False, delivery="realtime", watch_source="phone.meituan"),
        expires_at=time.time() + 3600,
    )
    updated = store.upsert_rule(custom)
    assert updated["rules_version"] == 2
    assert any(r["id"] == "meituan-order" for r in updated["rules"])

    ok, disabled = store.set_rule_enabled("meituan-order", False)
    assert ok is True
    assert disabled["rules_version"] == 3
    meituan = next(r for r in disabled["rules"] if r["id"] == "meituan-order")
    assert meituan["enabled"] is False

    # 过期规则不再下发
    expired_payload = store.get_rules_payload(now=time.time() + 7200)
    assert not any(r["id"] == "meituan-order" for r in expired_payload["rules"])

    removed, after_rm = store.remove_rule("meituan-order")
    assert removed is True
    assert after_rm["rules_version"] == 4


def test_store_ingest_idempotent_and_queries(tmp_path: Path) -> None:
    store = PhoneEventStore(tmp_path)
    now_ms = int(time.time() * 1000)
    raw = [
        {
            "event_id": "evt-1",
            "adapter": "notification",
            "event_type": "notification_posted",
            "rule_id": "wechat-notifications",
            "package_name": "com.tencent.mm",
            "title": "张三",
            "summary": "晚上一起吃饭",
            "ts_ms": now_ms - 5_000,
            "payload": {"channel_id": "msg"},
        },
        {
            "event_id": "evt-2",
            "adapter": "foreground",
            "event_type": "app_session",
            "rule_id": "app-sessions",
            "package_name": "com.twitter.android",
            "title": "X 前台停留 120s",
            "summary": "停留 120 秒",
            "ts_ms": now_ms - 2_000,
            "payload": {"duration_s": 120},
        },
    ]
    accepted = store.ingest_events("pixel-9", raw)
    assert len(accepted) == 2
    # 重复提交同 event_id 必须幂等忽略
    dup = store.ingest_events("pixel-9", raw)
    assert dup == []

    recent = store.query_recent_events(adapter="notification")
    assert recent["count"] == 1
    assert recent["events"][0]["title"] == "张三"

    sessions = store.query_app_sessions(package_name="com.twitter.android")
    assert sessions["count"] == 1
    assert sessions["events"][0]["payload"]["duration_s"] == 120

    state = store.query_current_state(device_id="pixel-9", stale_after_seconds=60)
    assert state["status"] == "observed"
    assert state["devices"]["pixel-9"]["foreground"]["status"] == "observed"

    # 测试 stale 判定
    old_ms = now_ms - 4_000_000
    store.ingest_events("pixel-9", [{
        "event_id": "evt-old",
        "adapter": "device",
        "event_type": "wifi",
        "ts_ms": old_ms,
        "title": "Wi-Fi",
        "summary": "Home",
    }])
    state2 = store.query_current_state(device_id="pixel-9", stale_after_seconds=60)
    assert state2["devices"]["pixel-9"]["device"]["status"] == "stale"


def test_http_events_and_rules_endpoints(tmp_path: Path) -> None:
    store = PhoneEventStore(tmp_path)
    bridge = PhoneBridge()
    ingested_batches: list[int] = []

    async def on_ingested(items) -> None:
        ingested_batches.append(len(items))

    app = create_phone_app(bridge, event_store=store, on_events_ingested=on_ingested)
    with TestClient(app) as client:
        rules_res = client.get("/api/phone/rules")
        assert rules_res.status_code == 200
        assert rules_res.json()["rules_version"] == 1

        ev_res = client.post(
            "/api/phone/events",
            json={
                "device_id": "pixel-9",
                "events": [
                    {
                        "event_id": "http-1",
                        "adapter": "media",
                        "event_type": "playing",
                        "rule_id": "media-playback",
                        "package_name": "com.qidian.QDReader",
                        "title": "正在播放：还能保送怪物职业学院？",
                        "summary": "第 1039 章",
                        "ts_ms": int(time.time() * 1000),
                    }
                ],
            },
        )
        assert ev_res.status_code == 200
        body = ev_res.json()
        assert body["ok"] is True
        assert body["accepted"] == 1
        assert ingested_batches == [1]

        # 幂等重传不重复触发回调
        ev_res2 = client.post(
            "/api/phone/events",
            json={
                "device_id": "pixel-9",
                "events": [{"event_id": "http-1", "adapter": "media", "title": "dup"}],
            },
        )
        assert ev_res2.status_code == 200
        assert ev_res2.json()["accepted"] == 0
        assert ingested_batches == [1]


@pytest.mark.asyncio
async def test_subscribe_tool_pushes_rules_and_information_provider(tmp_path: Path) -> None:
    store = PhoneEventStore(tmp_path)
    bridge = PhoneBridge()
    pushed_frames: list[dict[str, object]] = []

    async def send_json(frame: dict[str, object]) -> None:
        pushed_frames.append(frame)

    async def close_transport() -> None:
        return None

    await bridge.register_node(
        {
            "type": "hello",
            "supports_snapshot_ref": True,
            "supports_events": True,
            "device_id": "pixel-9",
        },
        send_json=send_json,
        close_transport=close_transport,
    )

    sub_tool = DeviceSubscribeTool(store, bridge)
    prepared = await sub_tool.prepare(
        {
            "action": "upsert",
            "ttl_seconds": 3600,
            "rule": {
                "id": "bili-watch",
                "adapter": "foreground",
                "package": "tv.danmaku.bili",
                "match": {"min_duration_s": 30},
                "actions": {"delivery": "batch", "watch_source": "phone.bili"},
            },
        }
    )
    assert isinstance(prepared, dict)
    res = await sub_tool.invoke("k1", prepared)
    assert res.outcome == "success"
    assert len(pushed_frames) == 1
    assert pushed_frames[0]["type"] == "rules"
    assert pushed_frames[0]["rules_version"] == 2

    provider = PhoneInformationProvider(store)
    desc = await provider.describe()
    assert "recent_events" in desc["queries"]
    rules_q = await provider.query("rules", {})
    assert any(r["id"] == "bili-watch" for r in rules_q["rules"])

    events_tool = PhoneEventsTool(store)
    ev_prep = await events_tool.prepare({"action": "current_state"})
    assert isinstance(ev_prep, dict)
    ev_res = await events_tool.invoke("k2", ev_prep)
    assert ev_res.outcome == "success"
