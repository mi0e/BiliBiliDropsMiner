from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from bilibili_drops_miner.client_parts.models import LiveRoomInfo, LiveTraceSession


def compact_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def hmac_by_rule(data: bytes, secret_key: str, rule: int) -> bytes:
    digest_map = {
        0: hashlib.md5,
        1: hashlib.sha1,
        2: hashlib.sha256,
        3: hashlib.sha224,
        4: hashlib.sha512,
        5: hashlib.sha384,
    }
    digest = digest_map.get(rule)
    if digest is None:
        raise ValueError(f"不支持的 secret_rule: {rule}")
    return (
        hmac.new(secret_key.encode("utf-8"), data, digest)
        .hexdigest()
        .encode("utf-8")
    )


def build_x25kn_signature(
    *,
    live_buvid: str,
    live_uuid: str,
    parent_area_id: int,
    area_id: int,
    seq_id: int,
    room_id: int,
    ets: int,
    duration: int,
    ts_ms: int,
    secret_key: str,
    secret_rule: list[int],
) -> str:
    payload = {
        "platform": "web",
        "parent_id": parent_area_id,
        "area_id": area_id,
        "seq_id": seq_id,
        "room_id": room_id,
        "buvid": live_buvid,
        "uuid": live_uuid,
        "ets": ets,
        "time": duration,
        "ts": ts_ms,
    }
    current = compact_json(payload).encode("utf-8")
    for rule in secret_rule:
        current = hmac_by_rule(current, secret_key, int(rule))
    return current.decode("utf-8")


def build_live_trace_enter_params(
    room: LiveRoomInfo,
    *,
    live_buvid: str,
    live_uuid: str,
    user_agent: str,
    csrf: str,
    ts_ms: int,
) -> dict[str, Any]:
    return {
        "id": compact_json([room.parent_area_id, room.area_id, 0, room.room_id]),
        "device": compact_json([live_buvid, live_uuid]),
        "ruid": room.ruid,
        "ts": ts_ms,
        "is_patch": 0,
        "heart_beat": "[]",
        "ua": user_agent,
        "web_location": "444.8",
        "csrf": csrf,
    }


def live_trace_session_from_enter_payload(
    payload: dict[str, Any],
    *,
    room: LiveRoomInfo,
) -> LiveTraceSession:
    data = payload.get("data") or {}

    heartbeat_interval = int(data.get("heartbeat_interval") or 60)
    ets = int(data.get("timestamp") or 0)
    secret_key = str(data.get("secret_key") or "")
    secret_rule_raw = data.get("secret_rule") or []
    secret_rule = [int(item) for item in secret_rule_raw]

    if not secret_key or not secret_rule:
        raise ValueError(f"x25Kn/E 返回缺少签名参数 room_id={room.room_id}")
    if heartbeat_interval <= 0:
        heartbeat_interval = 60
    if ets <= 0:
        raise ValueError(f"x25Kn/E 返回 timestamp 无效 room_id={room.room_id}")

    return LiveTraceSession(
        room_id=room.room_id,
        ruid=room.ruid,
        parent_area_id=room.parent_area_id,
        area_id=room.area_id,
        seq_id=0,
        ets=ets,
        heartbeat_interval=heartbeat_interval,
        secret_key=secret_key,
        secret_rule=secret_rule,
    )


def build_live_trace_heartbeat_params(
    session: LiveTraceSession,
    *,
    live_buvid: str,
    live_uuid: str,
    user_agent: str,
    csrf: str,
    signature: str,
    next_seq: int,
    duration: int,
    ts_ms: int,
) -> dict[str, Any]:
    return {
        "s": signature,
        "id": compact_json(
            [session.parent_area_id, session.area_id, next_seq, session.room_id]
        ),
        "device": compact_json([live_buvid, live_uuid]),
        "ruid": session.ruid,
        "ets": session.ets,
        "benchmark": session.secret_key,
        "time": duration,
        "ts": ts_ms,
        "trackid": -99998,
        "ua": user_agent,
        "web_location": "444.8",
        "csrf": csrf,
    }


def apply_live_trace_heartbeat_payload(
    session: LiveTraceSession,
    payload: dict[str, Any],
    *,
    next_seq: int,
) -> LiveTraceSession:
    data = payload.get("data") or {}
    heartbeat_interval = int(
        data.get("heartbeat_interval") or session.heartbeat_interval or 60
    )
    if heartbeat_interval <= 0:
        heartbeat_interval = 60

    session.seq_id = next_seq
    session.ets = int(data.get("timestamp") or session.ets)
    session.heartbeat_interval = heartbeat_interval

    next_secret_key = str(data.get("secret_key") or "").strip()
    next_secret_rule_raw = data.get("secret_rule")
    if next_secret_key:
        session.secret_key = next_secret_key
    if isinstance(next_secret_rule_raw, list) and next_secret_rule_raw:
        session.secret_rule = [int(item) for item in next_secret_rule_raw]
    return session
