from __future__ import annotations

import asyncio
import importlib.util
import json
import time
from pathlib import Path
import sys
from unittest.mock import AsyncMock


def _load_mods():
    root = Path(__file__).resolve().parents[1]
    if "phone_use_events_test_pkg" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "phone_use_events_test_pkg",
            root / "contract.py",
            submodule_search_locations=[str(root)],
        )
        assert spec is not None and spec.loader is not None
        pkg = importlib.util.module_from_spec(spec)
        sys.modules["phone_use_events_test_pkg"] = pkg
        spec.loader.exec_module(pkg)
        for mod in ("contract", "safety", "bridge", "events", "tools", "server"):
            m_spec = importlib.util.spec_from_file_location(
                f"phone_use_events_test_pkg.{mod}",
                root / f"{mod}.py",
            )
            assert m_spec is not None and m_spec.loader is not None
            m = importlib.util.module_from_spec(m_spec)
            sys.modules[f"phone_use_events_test_pkg.{mod}"] = m
            m_spec.loader.exec_module(m)
            setattr(pkg, mod, m)
    pkg = sys.modules["phone_use_events_test_pkg"]
    return pkg.events, pkg.tools, pkg.server


events_mod, tools_mod, server_mod = _load_mods()
PhoneEventStore = events_mod.PhoneEventStore
PhoneSubscriptionRule = events_mod.PhoneSubscriptionRule
default_rules = events_mod.default_rules
DeviceSubscribeTool = tools_mod.DeviceSubscribeTool


def test_rule_exclude_packages_validation_and_matching():
    rule = PhoneSubscriptionRule(
        id="appops-sensors",
        adapter="appops",
        package="*",
        exclude_packages=[" com.google.android.as ", "", "com.google.android.as"],
    )
    assert rule.exclude_packages == ["com.google.android.as"]
    wire = rule.to_wire()
    assert wire["exclude_packages"] == ["com.google.android.as"]
    assert not rule.matches_package("com.google.android.as")
    assert rule.matches_package("com.tencent.mm")

    inline = PhoneSubscriptionRule(
        id="inline",
        adapter="appops",
        package="*, !com.google.android.as",
    )
    assert not inline.matches_package("com.google.android.as")
    assert inline.matches_package("cn.kaiheila")


def test_legacy_rules_json_compatible(tmp_path: Path):
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        json.dumps(
            {
                "rules_version": 4,
                "rules": [
                    {
                        "id": "legacy-appops",
                        "enabled": True,
                        "adapter": "appops",
                        "package": "*",
                        "match": {"ops": ["RECORD_AUDIO"]},
                        "actions": {"delivery": "batch"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = PhoneEventStore(tmp_path)
    listed = store.list_rules()
    assert listed["rules_version"] == 4
    assert listed["rules"][0]["exclude_packages"] == []


def test_default_rules_block_pixel_asi_noise():
    rules = {r.id: r for r in default_rules()}
    appops = rules["appops-sensors"]
    assert appops.package == "*"
    assert "com.google.android.as" in appops.exclude_packages
    assert not appops.matches_package("com.google.android.as")
    assert appops.matches_package("com.tencent.mm")


def test_device_subscribe_persists_and_pushes_exclude_packages(tmp_path: Path):
    store = PhoneEventStore(tmp_path)
    bridge = AsyncMock()
    bridge.push_rules = AsyncMock(return_value=1)
    tool = DeviceSubscribeTool(store, bridge)

    args = asyncio.run(
        tool.prepare(
            {
                "action": "upsert",
                "rule": {
                    "id": "appops-sensors",
                    "adapter": "appops",
                    "package": "*",
                    "exclude_packages": ["com.google.android.as"],
                    "match": {"ops": ["FINE_LOCATION", "CAMERA", "RECORD_AUDIO"]},
                    "actions": {"cooldown_s": 120, "delivery": "batch"},
                },
            }
        )
    )
    assert isinstance(args, dict)
    res = asyncio.run(tool.invoke("k1", args))
    assert res.outcome == "success"
    bridge.push_rules.assert_awaited_once()
    pushed_payload = bridge.push_rules.call_args.args[0]
    appops = [r for r in pushed_payload["rules"] if r["id"] == "appops-sensors"][0]
    assert appops["exclude_packages"] == ["com.google.android.as"]

    reloaded = PhoneEventStore(tmp_path).find_rule("appops-sensors")
    assert reloaded is not None
    assert reloaded.exclude_packages == ["com.google.android.as"]


def test_purge_events_removes_only_target_noise(tmp_path: Path):
    store = PhoneEventStore(tmp_path)
    store.ingest_events(
        "dev-1",
        [
            {
                "event_id": "e1",
                "ts_ms": int(time.time() * 1000) - 2000,
                "adapter": "appops",
                "event_type": "op_start",
                "rule_id": "appops-sensors",
                "package_name": "com.google.android.as",
                "title": "Android System Intelligence 开启 RECORD_AUDIO",
                "summary": "RECORD_AUDIO active=true",
                "payload": {},
            },
            {
                "event_id": "e2",
                "ts_ms": int(time.time() * 1000) - 1000,
                "adapter": "notification",
                "event_type": "notification_posted",
                "rule_id": "wechat-notifications",
                "package_name": "com.tencent.mm",
                "title": "微信",
                "summary": "hi",
                "payload": {},
            },
        ],
    )
    removed = store.purge_events(package_name="com.google.android.as", adapter="appops")
    assert removed == 1
    recent = store.query_recent_events(since_hours=720)
    assert [e["event_id"] for e in recent["events"]] == ["e2"]


def test_bundled_apk_version_matches_file():
    root = Path(__file__).resolve().parents[1]
    assert server_mod.BUNDLED_SHELL_VERSION == "0.7.1"
    assert (root / f"akashic-shell-v{server_mod.BUNDLED_SHELL_VERSION}.apk").is_file()
