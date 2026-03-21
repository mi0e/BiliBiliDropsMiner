from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from bilibili_drops_miner.utils import get_cookie_value, join_cookie, parse_cookie

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class DanmuServerConf:
    room_id: int
    token: str
    host: str
    wss_port: int


@dataclass(slots=True)
class LiveRoomInfo:
    room_id: int
    ruid: int
    parent_area_id: int
    area_id: int
    live_status: int


@dataclass(slots=True)
class LiveTraceSession:
    room_id: int
    ruid: int
    parent_area_id: int
    area_id: int
    seq_id: int
    ets: int
    heartbeat_interval: int
    secret_key: str
    secret_rule: list[int]


@dataclass(slots=True)
class TaskProgress:
    task_id: str
    task_name: str
    status: int
    cur_value: int | float
    limit_value: int | float

    @property
    def is_completed(self) -> bool:
        # Bilibili task status uses positive values for completed/claimable states.
        if self.status >= 2:
            return True
        try:
            return float(self.limit_value) > 0 and float(self.cur_value) >= float(
                self.limit_value
            )
        except Exception:
            return False


class BilibiliClient:
    MIXIN_KEY_ENC_TAB = [
        46,
        47,
        18,
        2,
        53,
        8,
        23,
        32,
        15,
        50,
        10,
        31,
        58,
        3,
        45,
        35,
        27,
        43,
        5,
        49,
        33,
        9,
        42,
        19,
        29,
        28,
        14,
        39,
        12,
        38,
        41,
        13,
        37,
        48,
        7,
        16,
        24,
        55,
        40,
        61,
        26,
        17,
        0,
        1,
        60,
        51,
        30,
        4,
        22,
        25,
        54,
        21,
        56,
        59,
        6,
        63,
        57,
        62,
        11,
        36,
        20,
        34,
        44,
        52,
    ]

    def __init__(self, cookie: str) -> None:
        cookie_map = parse_cookie(cookie)
        if "buvid3" not in cookie_map:
            cookie_map["buvid3"] = f"{uuid.uuid4()}infoc"

        self.cookie_map = cookie_map
        self.cookie_header = join_cookie(cookie_map)
        self.bili_jct = get_cookie_value(self.cookie_header, "bili_jct")
        self.user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        self.live_buvid = cookie_map.get("LIVE_BUVID") or self._generate_live_buvid()
        self.live_uuid = str(uuid.uuid4())
        self._wbi_cache: tuple[str, str] | None = None

        self._http = httpx.AsyncClient(
            timeout=20.0,
            headers={
                "User-Agent": self.user_agent,
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
                "Cookie": self.cookie_header,
            },
        )

    def update_cookie(self, cookie: str) -> None:
        cookie_map = parse_cookie(cookie)
        if "buvid3" not in cookie_map:
            cookie_map["buvid3"] = self.cookie_map.get(
                "buvid3", f"{uuid.uuid4()}infoc"
            )
        self.cookie_map = cookie_map
        self.cookie_header = join_cookie(cookie_map)
        self.bili_jct = get_cookie_value(self.cookie_header, "bili_jct")
        self._http.headers["Cookie"] = self.cookie_header
        self._wbi_cache = None

    async def close(self) -> None:
        await self._http.aclose()

    async def nav(self) -> dict[str, Any]:
        response = await self._http.get("https://api.bilibili.com/x/web-interface/nav")
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") not in (0, -101):
            raise ValueError(f"登录状态异常: {json.dumps(payload, ensure_ascii=False)}")
        return payload

    async def get_self_info(self) -> tuple[int | None, str]:
        payload = await self.nav()
        data = payload.get("data") or {}
        is_login = bool(data.get("isLogin"))
        if not is_login:
            return None, ""
        uid = int(data.get("mid") or 0)
        uname = str(data.get("uname") or "")
        if uid <= 0:
            return None, uname
        return uid, uname

    async def get_wbi_keys(self) -> tuple[str, str]:
        if self._wbi_cache is not None:
            return self._wbi_cache
        payload = await self.nav()
        data = payload.get("data") or {}
        wbi_img = data.get("wbi_img") or {}
        img_url = str(wbi_img.get("img_url", ""))
        sub_url = str(wbi_img.get("sub_url", ""))
        if not img_url or not sub_url:
            raise ValueError("nav 返回缺少 wbi_img")
        img_key = img_url.rsplit("/", 1)[-1].split(".", 1)[0]
        sub_key = sub_url.rsplit("/", 1)[-1].split(".", 1)[0]
        if not img_key or not sub_key:
            raise ValueError("wbi key 解析失败")
        self._wbi_cache = (img_key, sub_key)
        return img_key, sub_key

    @classmethod
    def _get_mixin_key(cls, img_key: str, sub_key: str) -> str:
        raw = img_key + sub_key
        return "".join(raw[index] for index in cls.MIXIN_KEY_ENC_TAB)[:32]

    @classmethod
    def _encode_query(cls, params: dict[str, Any]) -> str:
        filtered = {
            key: "".join(ch for ch in str(value) if ch not in "!'()*")
            for key, value in params.items()
        }
        encoded_items = []
        for key, value in filtered.items():
            encoded_key = urllib.parse.quote(str(key), safe="")
            encoded_value = urllib.parse.quote(str(value), safe="")
            encoded_items.append(f"{encoded_key}={encoded_value}")
        return "&".join(encoded_items)

    async def sign_wbi(self, params: dict[str, Any]) -> dict[str, Any]:
        img_key, sub_key = await self.get_wbi_keys()
        mixin_key = self._get_mixin_key(img_key, sub_key)
        signed = dict(params)
        signed["wts"] = int(time.time())
        sorted_items = dict(sorted(signed.items(), key=lambda item: item[0]))
        query = self._encode_query(sorted_items)
        signed["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
        return signed

    @staticmethod
    def _generate_live_buvid() -> str:
        numeric = uuid.uuid4().int % (10**16)
        return f"AUTO{numeric:016d}"

    @staticmethod
    def _compact_json(value: Any) -> str:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)

    def _live_headers(self, room_id: int, *, lite: bool = False) -> dict[str, str]:
        referer = f"https://live.bilibili.com/{room_id}"
        if lite:
            referer = f"https://live.bilibili.com/blanc/{room_id}?liteVersion=true"
        return {
            "Referer": referer,
            "Origin": "https://live.bilibili.com",
            "User-Agent": self.user_agent,
            "Cookie": self.cookie_header,
        }

    async def _signed_get_json(
        self,
        url: str,
        params: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = False,
        retry_on_wbi_miss: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        retries = 2 if retry_on_wbi_miss else 1
        for retry in range(retries):
            signed_params = await self.sign_wbi(params)
            response = await self._http.get(
                url,
                params=signed_params,
                headers=headers,
                follow_redirects=follow_redirects,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") == 0:
                return payload
            if retry == 0 and retry_on_wbi_miss:
                self._wbi_cache = None
        return payload

    async def _signed_post_json(
        self,
        url: str,
        params: dict[str, Any],
        body: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        retry_on_wbi_miss: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        retries = 2 if retry_on_wbi_miss else 1
        for retry in range(retries):
            signed_params = await self.sign_wbi(params)
            response = await self._http.post(
                url,
                params=signed_params,
                json=body,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") == 0:
                return payload
            if retry == 0 and retry_on_wbi_miss:
                self._wbi_cache = None
        return payload

    async def _signed_post_query_json(
        self,
        url: str,
        params: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = False,
        retry_on_wbi_miss: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        retries = 2 if retry_on_wbi_miss else 1
        for retry in range(retries):
            signed_params = await self.sign_wbi(params)
            response = await self._http.post(
                url,
                params=signed_params,
                headers=headers,
                follow_redirects=follow_redirects,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") == 0:
                return payload
            if retry == 0 and retry_on_wbi_miss:
                self._wbi_cache = None
        return payload

    async def get_danmu_server(self, room_id: int) -> DanmuServerConf:
        payload = await self._signed_get_json(
            "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo",
            {
                "id": room_id,
                "type": 0,
                "web_location": "444.8",
            },
            headers=self._live_headers(room_id),
            retry_on_wbi_miss=True,
        )
        if payload.get("code") != 0:
            raise ValueError(
                f"获取弹幕配置失败 room_id={room_id}: {payload.get('message')}"
            )
        data = payload.get("data") or {}
        host_list = data.get("host_list") or []
        if not host_list:
            raise ValueError(f"弹幕 host_list 为空 room_id={room_id}")
        host_item = host_list[0]
        return DanmuServerConf(
            room_id=room_id,
            token=str(data["token"]),
            host=str(
                host_item.get("host")
                or data.get("host")
                or "broadcastlv.chat.bilibili.com"
            ),
            wss_port=int(host_item.get("wss_port", 443)),
        )

    async def get_live_room_info(self, room_id: int) -> LiveRoomInfo:
        payload = await self._signed_get_json(
            "https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom",
            {
                "room_id": room_id,
                "web_location": "444.8",
            },
            headers=self._live_headers(room_id),
            retry_on_wbi_miss=True,
        )
        if payload.get("code") != 0:
            raise ValueError(
                f"获取直播间信息失败 room_id={room_id}: {payload.get('message')}"
            )
        data = payload.get("data") or {}
        room_info = data.get("room_info") or {}

        real_room_id = int(room_info.get("room_id") or room_id)
        ruid = int(room_info.get("uid") or 0)
        parent_area_id = int(room_info.get("parent_area_id") or 0)
        area_id = int(room_info.get("area_id") or 0)
        live_status = int(room_info.get("live_status") or 0)

        if real_room_id <= 0 or ruid <= 0:
            raise ValueError(f"直播间信息不完整 room_id={room_id}")
        if parent_area_id <= 0 or area_id <= 0:
            raise ValueError(
                f"直播分区信息缺失 room_id={room_id} parent_area_id={parent_area_id} area_id={area_id}"
            )
        return LiveRoomInfo(
            room_id=real_room_id,
            ruid=ruid,
            parent_area_id=parent_area_id,
            area_id=area_id,
            live_status=live_status,
        )

    async def room_entry_action(self, room_id: int) -> None:
        if not self.bili_jct:
            raise ValueError("cookie 缺少 bili_jct，无法上报 roomEntryAction")
        payload = await self._signed_post_json(
            "https://api.live.bilibili.com/xlive/web-room/v1/index/roomEntryAction",
            {"csrf": self.bili_jct},
            {"room_id": room_id, "platform": "pc"},
            headers=self._live_headers(room_id),
            retry_on_wbi_miss=True,
        )
        if payload.get("code") != 0:
            raise ValueError(
                f"roomEntryAction 失败 room_id={room_id}: {payload.get('message')}"
            )

    @staticmethod
    def _hmac_by_rule(data: bytes, secret_key: str, rule: int) -> bytes:
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

    def _build_x25kn_signature(
        self,
        *,
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
            "buvid": self.live_buvid,
            "uuid": self.live_uuid,
            "ets": ets,
            "time": duration,
            "ts": ts_ms,
        }
        current = self._compact_json(payload).encode("utf-8")
        for rule in secret_rule:
            current = self._hmac_by_rule(current, secret_key, int(rule))
        return current.decode("utf-8")

    async def live_trace_enter(self, room_id: int) -> LiveTraceSession:
        if not self.bili_jct:
            raise ValueError("cookie 缺少 bili_jct，无法初始化 x25Kn 心跳")

        room = await self.get_live_room_info(room_id)
        if room.live_status != 1:
            LOGGER.warning(
                "room=%s 当前状态非开播 live_status=%s，计时可能不会增长",
                room.room_id,
                room.live_status,
            )

        params: dict[str, Any] = {
            "id": self._compact_json(
                [room.parent_area_id, room.area_id, 0, room.room_id]
            ),
            "device": self._compact_json([self.live_buvid, self.live_uuid]),
            "ruid": room.ruid,
            "ts": int(time.time() * 1000),
            "is_patch": 0,
            "heart_beat": "[]",
            "ua": self.user_agent,
            "web_location": "444.8",
            "csrf": self.bili_jct,
        }
        payload = await self._signed_post_query_json(
            "https://live-trace.bilibili.com/xlive/data-interface/v1/x25Kn/E",
            params,
            headers=self._live_headers(room.room_id, lite=True),
            retry_on_wbi_miss=True,
        )
        if payload.get("code") != 0:
            raise ValueError(
                f"x25Kn/E 失败 room_id={room.room_id}: {payload.get('message')}"
            )
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

    async def live_trace_heartbeat(self, session: LiveTraceSession) -> LiveTraceSession:
        if not self.bili_jct:
            raise ValueError("cookie 缺少 bili_jct，无法发送 x25Kn/X")
        if not session.secret_key or not session.secret_rule:
            raise ValueError("x25Kn 会话缺少 secret_key/secret_rule")

        next_seq = session.seq_id + 1
        duration = max(1, int(session.heartbeat_interval))
        ts_ms = int(time.time() * 1000)
        signature = self._build_x25kn_signature(
            parent_area_id=session.parent_area_id,
            area_id=session.area_id,
            seq_id=next_seq,
            room_id=session.room_id,
            ets=session.ets,
            duration=duration,
            ts_ms=ts_ms,
            secret_key=session.secret_key,
            secret_rule=session.secret_rule,
        )

        params: dict[str, Any] = {
            "s": signature,
            "id": self._compact_json(
                [session.parent_area_id, session.area_id, next_seq, session.room_id]
            ),
            "device": self._compact_json([self.live_buvid, self.live_uuid]),
            "ruid": session.ruid,
            "ets": session.ets,
            "benchmark": session.secret_key,
            "time": duration,
            "ts": ts_ms,
            "trackid": -99998,
            "ua": self.user_agent,
            "web_location": "444.8",
            "csrf": self.bili_jct,
        }
        payload = await self._signed_post_query_json(
            "https://live-trace.bilibili.com/xlive/data-interface/v1/x25Kn/X",
            params,
            headers=self._live_headers(session.room_id, lite=True),
            follow_redirects=True,
            retry_on_wbi_miss=True,
        )
        if payload.get("code") != 0:
            raise ValueError(
                f"x25Kn/X 失败 room_id={session.room_id}: {payload.get('message')}"
            )

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

    async def get_task_progress(self, task_ids: list[str]) -> list[TaskProgress]:
        normalized_ids = [task_id.strip() for task_id in task_ids if task_id.strip()]
        if not normalized_ids:
            return []
        if not self.bili_jct:
            raise ValueError("cookie 缺少 bili_jct，无法查询任务进度")

        payload = await self._signed_get_json(
            "https://api.bilibili.com/x/task/totalv2",
            {
                "csrf": self.bili_jct,
                "task_ids": ",".join(normalized_ids),
                "web_location": "0.0",
            },
            headers={
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
                "User-Agent": self.user_agent,
                "Cookie": self.cookie_header,
            },
            retry_on_wbi_miss=True,
        )
        if payload.get("code") != 0:
            raise ValueError(f"查询任务进度失败: {payload.get('message')}")

        data = payload.get("data") or {}
        task_list = data.get("list") or []
        progresses: list[TaskProgress] = []
        for item in task_list:
            task_id = str(item.get("task_id") or "")
            task_name = str(item.get("task_name") or task_id)
            status = int(item.get("task_status") or 0)
            indicators = item.get("indicators") or []
            cur_value: int | float = 0
            limit_value: int | float = 0
            if indicators and isinstance(indicators[0], dict):
                indicator = indicators[0]
                cur_value = indicator.get("cur_value") or 0
                limit_value = indicator.get("limit") or 0
            progresses.append(
                TaskProgress(
                    task_id=task_id,
                    task_name=task_name,
                    status=status,
                    cur_value=cur_value,
                    limit_value=limit_value,
                )
            )
        return progresses
