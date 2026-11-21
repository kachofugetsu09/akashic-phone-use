"""Phone 端侧通用事件流存储、声明式订阅规则与 Watch / UserInformation 适配。"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError

AdapterKind = Literal["notification", "foreground", "appops", "media", "device"]
DeliveryKind = Literal["realtime", "batch"]
RedactKind = Literal["full", "summary", "metadata_only"]

DEFAULT_TIMEZONE = "Asia/Shanghai"
_MAX_QUERY_LIMIT = 100


class RuleMatchConfig(BaseModel):
    model_config = ConfigDict(extra="allow", strict=False)
    title_regex: str | None = None
    text_regex: str | None = None
    ignore_ongoing: bool = True
    ops: list[str] | None = None
    min_duration_s: int = Field(default=0, ge=0, le=86_400)


class RuleActionsConfig(BaseModel):
    model_config = ConfigDict(extra="allow", strict=False)
    auto_cancel: bool = False
    redact: RedactKind = "full"
    emit_watch: bool = True
    watch_source: str | None = None
    watch_state: bool = False
    cooldown_s: int = Field(default=0, ge=0, le=86_400)
    delivery: DeliveryKind = "batch"
    inspect_on_exit: bool = False


class PhoneSubscriptionRule(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False)
    id: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    adapter: AdapterKind
    package: str | None = Field(default="*", max_length=512)
    match: RuleMatchConfig = Field(default_factory=RuleMatchConfig)
    actions: RuleActionsConfig = Field(default_factory=RuleActionsConfig)
    expires_at: float | None = Field(default=None, ge=0)

    def to_wire(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def default_rules() -> list[PhoneSubscriptionRule]:
    return [
        PhoneSubscriptionRule(
            id="wechat-notifications",
            enabled=True,
            adapter="notification",
            package="com.tencent.mm",
            match=RuleMatchConfig(ignore_ongoing=True),
            actions=RuleActionsConfig(
                auto_cancel=True,
                redact="full",
                emit_watch=True,
                watch_source="phone.wechat",
                watch_state=False,
                cooldown_s=0,
                delivery="realtime",
            ),
        ),
        PhoneSubscriptionRule(
            id="app-sessions",
            enabled=True,
            adapter="foreground",
            package="*",
            match=RuleMatchConfig(min_duration_s=15),
            actions=RuleActionsConfig(
                redact="full",
                emit_watch=True,
                watch_source="phone.app",
                watch_state=False,
                cooldown_s=0,
                delivery="batch",
            ),
        ),
        PhoneSubscriptionRule(
            id="appops-sensors",
            enabled=True,
            adapter="appops",
            package="*",
            match=RuleMatchConfig(ops=["FINE_LOCATION", "CAMERA", "RECORD_AUDIO"]),
            actions=RuleActionsConfig(
                redact="full",
                emit_watch=True,
                watch_source="phone.appops",
                watch_state=False,
                cooldown_s=10,
                delivery="batch",
            ),
        ),
        PhoneSubscriptionRule(
            id="media-playback",
            enabled=True,
            adapter="media",
            package="*",
            match=RuleMatchConfig(),
            actions=RuleActionsConfig(
                redact="full",
                emit_watch=True,
                watch_source="phone.media",
                watch_state=True,
                cooldown_s=5,
                delivery="batch",
            ),
        ),
        PhoneSubscriptionRule(
            id="device-context",
            enabled=True,
            adapter="device",
            package=None,
            match=RuleMatchConfig(),
            actions=RuleActionsConfig(
                redact="full",
                emit_watch=True,
                watch_source="phone.device",
                watch_state=True,
                cooldown_s=5,
                delivery="batch",
            ),
        ),
    ]


@dataclass(frozen=True, slots=True)
class StoredPhoneEvent:
    device_id: str
    event_id: str
    ts_ms: int
    adapter: str
    event_type: str
    rule_id: str
    package_name: str
    title: str
    summary: str
    payload: dict[str, object]


class RecentEventsParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    adapter: AdapterKind | None = None
    package_name: str | None = Field(default=None, min_length=1, max_length=256)
    device_id: str | None = Field(default=None, min_length=1, max_length=128)
    since_hours: float = Field(default=24.0, gt=0, le=720.0)
    limit: int = Field(default=50, ge=1, le=_MAX_QUERY_LIMIT)


class AppSessionsParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    package_name: str | None = Field(default=None, min_length=1, max_length=256)
    device_id: str | None = Field(default=None, min_length=1, max_length=128)
    since_hours: float = Field(default=24.0, gt=0, le=720.0)
    limit: int = Field(default=50, ge=1, le=_MAX_QUERY_LIMIT)


class CurrentStateParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    device_id: str | None = Field(default=None, min_length=1, max_length=128)
    stale_after_seconds: int = Field(default=1800, ge=30, le=86_400)


class EmptyParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PhoneEventStore:
    """持久化手机端侧订阅规则与多适配器事件流（SQLite + 原子 rules.json）。"""

    def __init__(self, data_root: Path, *, timezone: str = DEFAULT_TIMEZONE) -> None:
        self.data_root = Path(data_root)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.tz = ZoneInfo(timezone)
        self._db_path = self.data_root / "events.sqlite3"
        self._rules_path = self.data_root / "rules.json"
        self._init_db()
        self._ensure_rules_file()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    ts_ms INTEGER NOT NULL,
                    adapter TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    rule_id TEXT NOT NULL,
                    package_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    received_at_ms INTEGER NOT NULL,
                    UNIQUE(device_id, event_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts_ms DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_adapter_ts ON events(adapter, ts_ms DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS device_state (
                    device_id TEXT NOT NULL,
                    slot TEXT NOT NULL,
                    ts_ms INTEGER NOT NULL,
                    package_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(device_id, slot)
                )
                """
            )

    def _ensure_rules_file(self) -> None:
        if self._rules_path.exists():
            return
        self._write_rules_state(1, default_rules())

    def _read_rules_state(self) -> tuple[int, list[PhoneSubscriptionRule]]:
        if not self._rules_path.exists():
            rules = default_rules()
            self._write_rules_state(1, rules)
            return 1, rules
        raw = json.loads(self._rules_path.read_text(encoding="utf-8"))
        version = int(raw.get("rules_version") or 1)
        rules: list[PhoneSubscriptionRule] = []
        for item in raw.get("rules") or []:
            try:
                rules.append(PhoneSubscriptionRule.model_validate(item))
            except ValidationError:
                continue
        return version, rules

    def _write_rules_state(self, version: int, rules: list[PhoneSubscriptionRule]) -> None:
        payload = {
            "rules_version": version,
            "updated_at": datetime.now(UTC).isoformat(),
            "rules": [rule.to_wire() for rule in rules],
        }
        tmp = self._rules_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self._rules_path)

    def get_rules_payload(self, *, now: float | None = None) -> dict[str, object]:
        now_s = time.time() if now is None else now
        version, rules = self._read_rules_state()
        active = [
            rule.to_wire()
            for rule in rules
            if rule.expires_at is None or rule.expires_at > now_s
        ]
        return {"rules_version": version, "rules": active}

    def list_rules(self, *, include_expired: bool = False) -> dict[str, object]:
        now_s = time.time()
        version, rules = self._read_rules_state()
        items = [
            rule.to_wire()
            for rule in rules
            if include_expired or rule.expires_at is None or rule.expires_at > now_s
        ]
        return {"rules_version": version, "rules": items}

    def upsert_rule(self, rule: PhoneSubscriptionRule) -> dict[str, object]:
        version, rules = self._read_rules_state()
        now_s = time.time()
        kept = [
            existing for existing in rules
            if existing.id != rule.id and (existing.expires_at is None or existing.expires_at > now_s)
        ]
        kept.append(rule)
        new_version = version + 1
        self._write_rules_state(new_version, kept)
        return self.get_rules_payload(now=now_s)

    def remove_rule(self, rule_id: str) -> tuple[bool, dict[str, object]]:
        version, rules = self._read_rules_state()
        kept = [existing for existing in rules if existing.id != rule_id]
        if len(kept) == len(rules):
            return False, self.get_rules_payload()
        self._write_rules_state(version + 1, kept)
        return True, self.get_rules_payload()

    def set_rule_enabled(self, rule_id: str, enabled: bool) -> tuple[bool, dict[str, object]]:
        version, rules = self._read_rules_state()
        found = False
        updated: list[PhoneSubscriptionRule] = []
        for existing in rules:
            if existing.id == rule_id:
                found = True
                updated.append(existing.model_copy(update={"enabled": enabled}))
            else:
                updated.append(existing)
        if not found:
            return False, self.get_rules_payload()
        self._write_rules_state(version + 1, updated)
        return True, self.get_rules_payload()

    def find_rule(self, rule_id: str) -> PhoneSubscriptionRule | None:
        _, rules = self._read_rules_state()
        for rule in rules:
            if rule.id == rule_id:
                return rule
        return None

    def ingest_events(
        self,
        device_id: str,
        raw_events: list[Mapping[str, object]],
    ) -> list[StoredPhoneEvent]:
        if not raw_events:
            return []
        now_ms = int(time.time() * 1000)
        accepted: list[StoredPhoneEvent] = []
        with self._connect() as conn:
            for item in raw_events:
                event_id = str(item.get("event_id") or "").strip()[:128]
                adapter = str(item.get("adapter") or "").strip()[:40]
                if not event_id or not adapter:
                    continue
                raw_ts = item.get("ts_ms")
                ts_ms = int(raw_ts) if isinstance(raw_ts, (int, float)) and not isinstance(raw_ts, bool) else now_ms
                event_type = str(item.get("event_type") or adapter).strip()[:64]
                rule_id = str(item.get("rule_id") or "").strip()[:100]
                package_name = str(item.get("package_name") or "").strip()[:256]
                title = str(item.get("title") or "").strip()[:500]
                summary = str(item.get("summary") or item.get("text") or "").strip()[:2000]
                raw_payload = item.get("payload")
                payload_dict = (
                    {str(k): v for k, v in cast(Mapping[str, object], raw_payload).items()}
                    if isinstance(raw_payload, Mapping)
                    else {}
                )
                payload_json = json.dumps(payload_dict, ensure_ascii=False, separators=(",", ":"))
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO events (
                        device_id, event_id, ts_ms, adapter, event_type,
                        rule_id, package_name, title, summary, payload_json, received_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        device_id, event_id, ts_ms, adapter, event_type,
                        rule_id, package_name, title, summary, payload_json, now_ms,
                    ),
                )
                if cur.rowcount > 0:
                    accepted.append(
                        StoredPhoneEvent(
                            device_id=device_id,
                            event_id=event_id,
                            ts_ms=ts_ms,
                            adapter=adapter,
                            event_type=event_type,
                            rule_id=rule_id,
                            package_name=package_name,
                            title=title,
                            summary=summary,
                            payload=payload_dict,
                        )
                    )
                if adapter in ("foreground", "media", "device"):
                    slot = adapter
                    existing = conn.execute(
                        "SELECT ts_ms FROM device_state WHERE device_id = ? AND slot = ?",
                        (device_id, slot),
                    ).fetchone()
                    if existing is None or int(existing["ts_ms"]) <= ts_ms:
                        conn.execute(
                            """
                            INSERT INTO device_state (
                                device_id, slot, ts_ms, package_name, title, summary, payload_json, updated_at_ms
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(device_id, slot) DO UPDATE SET
                                ts_ms=excluded.ts_ms,
                                package_name=excluded.package_name,
                                title=excluded.title,
                                summary=excluded.summary,
                                payload_json=excluded.payload_json,
                                updated_at_ms=excluded.updated_at_ms
                            """,
                            (device_id, slot, ts_ms, package_name, title, summary, payload_json, now_ms),
                        )
        return accepted

    def _iso(self, ts_ms: int) -> str:
        return datetime.fromtimestamp(ts_ms / 1000.0, self.tz).isoformat()

    def query_recent_events(
        self,
        *,
        adapter: str | None = None,
        package_name: str | None = None,
        device_id: str | None = None,
        since_hours: float = 24.0,
        limit: int = 50,
    ) -> dict[str, object]:
        cutoff_ms = int((time.time() - since_hours * 3600.0) * 1000)
        clauses = ["ts_ms >= ?"]
        args: list[Any] = [cutoff_ms]
        if adapter:
            clauses.append("adapter = ?")
            args.append(adapter)
        if package_name:
            clauses.append("package_name = ?")
            args.append(package_name)
        if device_id:
            clauses.append("device_id = ?")
            args.append(device_id)
        bounded = max(1, min(limit, _MAX_QUERY_LIMIT))
        args.append(bounded)
        sql = (
            "SELECT device_id, event_id, ts_ms, adapter, event_type, rule_id, "
            "package_name, title, summary, payload_json "
            f"FROM events WHERE {' AND '.join(clauses)} "
            "ORDER BY ts_ms DESC, id DESC LIMIT ?"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        items = [
            {
                "device_id": row["device_id"],
                "event_id": row["event_id"],
                "ts_ms": row["ts_ms"],
                "ts_iso": self._iso(int(row["ts_ms"])),
                "adapter": row["adapter"],
                "event_type": row["event_type"],
                "rule_id": row["rule_id"],
                "package_name": row["package_name"],
                "title": row["title"],
                "summary": row["summary"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]
        return {
            "queried_at": datetime.now(UTC).isoformat(),
            "count": len(items),
            "events": items,
        }

    def query_app_sessions(
        self,
        *,
        package_name: str | None = None,
        device_id: str | None = None,
        since_hours: float = 24.0,
        limit: int = 50,
    ) -> dict[str, object]:
        return self.query_recent_events(
            adapter="foreground",
            package_name=package_name,
            device_id=device_id,
            since_hours=since_hours,
            limit=limit,
        )

    def query_current_state(
        self,
        *,
        device_id: str | None = None,
        stale_after_seconds: int = 1800,
    ) -> dict[str, object]:
        now = datetime.now(UTC)
        now_ms = int(now.timestamp() * 1000)
        with self._connect() as conn:
            if device_id:
                rows = conn.execute(
                    "SELECT * FROM device_state WHERE device_id = ? ORDER BY slot ASC",
                    (device_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM device_state ORDER BY device_id ASC, slot ASC"
                ).fetchall()
        devices: dict[str, dict[str, object]] = {}
        for row in rows:
            dev = str(row["device_id"])
            ts_ms = int(row["ts_ms"])
            age_s = max(0.0, (now_ms - ts_ms) / 1000.0)
            status = "stale" if age_s > stale_after_seconds else "observed"
            devices.setdefault(dev, {})[str(row["slot"])] = {
                "status": status,
                "age_seconds": round(age_s, 1),
                "observed_at_iso": self._iso(ts_ms),
                "package_name": row["package_name"],
                "title": row["title"],
                "summary": row["summary"],
                "payload": json.loads(row["payload_json"]),
            }
        return {
            "queried_at": now.isoformat(),
            "status": "unknown" if not devices else "observed",
            "devices": devices,
        }


class PhoneInformationProvider:
    """暴露给 userinformation 的统一只读证据接口。"""

    def __init__(self, store: PhoneEventStore) -> None:
        self._store = store

    async def describe(self) -> Mapping[str, object]:
        return {
            "queries": {
                "recent_events": {
                    "description": "按适配器、包名或时间窗查询手机端侧采集到的事件（通知、前台会话、AppOps 传感器、媒体播放、网络/亮屏等）。",
                    "parameters": RecentEventsParams.model_json_schema(),
                },
                "app_sessions": {
                    "description": "查询手机前台应用会话切换记录（含停留时长，供时间日志与活动梳理参考）。",
                    "parameters": AppSessionsParams.model_json_schema(),
                },
                "current_state": {
                    "description": "读取手机最近一次上报的 foreground / media / device 状态；附 age_seconds 与 observed/stale 状态标记。",
                    "parameters": CurrentStateParams.model_json_schema(),
                },
                "rules": {
                    "description": "查看当前生效的端侧事件订阅规则列表。",
                    "parameters": EmptyParams.model_json_schema(),
                },
            }
        }

    async def query(self, query: str, arguments: Mapping[str, object]) -> Mapping[str, object]:
        try:
            if query == "recent_events":
                p = RecentEventsParams.model_validate(dict(arguments))
                return self._store.query_recent_events(
                    adapter=p.adapter,
                    package_name=p.package_name,
                    device_id=p.device_id,
                    since_hours=p.since_hours,
                    limit=p.limit,
                )
            if query == "app_sessions":
                p = AppSessionsParams.model_validate(dict(arguments))
                return self._store.query_app_sessions(
                    package_name=p.package_name,
                    device_id=p.device_id,
                    since_hours=p.since_hours,
                    limit=p.limit,
                )
            if query == "current_state":
                p = CurrentStateParams.model_validate(dict(arguments))
                return self._store.query_current_state(
                    device_id=p.device_id,
                    stale_after_seconds=p.stale_after_seconds,
                )
            if query == "rules":
                _ = EmptyParams.model_validate(dict(arguments))
                return self._store.list_rules()
        except ValidationError:
            return {"error": "Invalid query arguments; see provider schema"}
        return {"error": f"Unknown query: {query}"}
