from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

import httpx

from bilibili_drops_miner.client_parts.constants import (
    WBI_MIXIN_KEY_ENC_TAB,
)
from bilibili_drops_miner.client_parts.cookies import (
    DEFAULT_USER_AGENT,
    build_cookie_state,
    build_default_headers,
    build_live_headers,
    build_mission_headers,
    generate_live_buvid,
)
from bilibili_drops_miner.client_parts.http import (
    is_rate_limited_payload,
    request_with_transient_retry,
    signed_get_json,
    signed_post_form_json,
    signed_post_json,
    signed_post_query_json,
)
from bilibili_drops_miner.client_parts.live import (
    parse_danmu_server_conf,
    parse_guard_active_watch_time,
    parse_live_room_info,
    parse_room_owner_uid,
)
from bilibili_drops_miner.client_parts.live_trace import (
    apply_live_trace_heartbeat_payload,
    build_live_trace_enter_params,
    build_live_trace_heartbeat_params,
    build_x25kn_signature,
    compact_json,
    hmac_by_rule,
    live_trace_session_from_enter_payload,
)
from bilibili_drops_miner.client_parts.models import (
    DanmuServerConf,
    LiveRoomInfo,
    LiveTraceSession,
    LiveWatchTime,
    MissionRewardClaimResult,
    MissionRewardInfo,
    TaskProgress,
)
from bilibili_drops_miner.client_parts.profile import (
    parse_self_info,
    validate_nav_payload,
)
from bilibili_drops_miner.client_parts.rewards import (
    MISSION_RETRY_DELAYS,
    REWARD_CLAIM_INTERVAL_SECONDS,
    build_reward_receive_body,
    failed_reward_claim_result,
    normalize_task_id,
    parse_mission_reward_info,
    reward_claim_result_from_payload,
    skipped_reward_claim_result,
)
from bilibili_drops_miner.client_parts.task_parsing import (
    coerce_task_number,
    extract_task_indicator_values,
)
from bilibili_drops_miner.client_parts.tasks import (
    normalize_task_ids,
    parse_task_progress_payload,
)
from bilibili_drops_miner.client_parts.wbi import (
    encode_query,
    get_mixin_key,
    parse_wbi_keys_from_nav,
    sign_wbi_params,
)

LOGGER = logging.getLogger(__name__)


_coerce_task_number = coerce_task_number
_extract_task_indicator_values = extract_task_indicator_values


class BilibiliClient:
    MIXIN_KEY_ENC_TAB = WBI_MIXIN_KEY_ENC_TAB

    def __init__(self, cookie: str) -> None:
        self.cookie_map, self.cookie_header, self.bili_jct = build_cookie_state(cookie)
        self.user_agent = DEFAULT_USER_AGENT
        self.live_buvid = self._generate_live_buvid()
        self.live_uuid = str(uuid.uuid4())
        self._wbi_cache: tuple[str, str] | None = None

        self._http = httpx.AsyncClient(
            timeout=20.0,
            headers=build_default_headers(self.user_agent, self.cookie_header),
        )

    def update_cookie(self, cookie: str) -> None:
        self.cookie_map, self.cookie_header, self.bili_jct = build_cookie_state(
            cookie,
            fallback_buvid3=self.cookie_map.get("buvid3"),
        )
        self._http.headers["Cookie"] = self.cookie_header
        self._wbi_cache = None

    async def close(self) -> None:
        await self._http.aclose()

    async def nav(self) -> dict[str, Any]:
        response = await self._http.get("https://api.bilibili.com/x/web-interface/nav")
        response.raise_for_status()
        payload = response.json()
        validate_nav_payload(payload)
        return payload

    async def get_self_info(self) -> tuple[int | None, str]:
        payload = await self.nav()
        return parse_self_info(payload)

    async def get_wbi_keys(self) -> tuple[str, str]:
        if self._wbi_cache is not None:
            return self._wbi_cache
        payload = await self.nav()
        img_key, sub_key = parse_wbi_keys_from_nav(payload)
        self._wbi_cache = (img_key, sub_key)
        return img_key, sub_key

    @classmethod
    def _get_mixin_key(cls, img_key: str, sub_key: str) -> str:
        return get_mixin_key(img_key, sub_key)

    @classmethod
    def _encode_query(cls, params: dict[str, Any]) -> str:
        return encode_query(params)

    async def sign_wbi(self, params: dict[str, Any]) -> dict[str, Any]:
        img_key, sub_key = await self.get_wbi_keys()
        return sign_wbi_params(
            params,
            img_key=img_key,
            sub_key=sub_key,
            timestamp=int(time.time()),
        )

    @staticmethod
    def _generate_live_buvid() -> str:
        return generate_live_buvid()

    @staticmethod
    def _compact_json(value: Any) -> str:
        return compact_json(value)

    def _live_headers(self, room_id: int, *, lite: bool = False) -> dict[str, str]:
        return build_live_headers(
            room_id,
            user_agent=self.user_agent,
            cookie_header=self.cookie_header,
            lite=lite,
        )

    def _mission_headers(self, task_id: str) -> dict[str, str]:
        return build_mission_headers(
            task_id,
            user_agent=self.user_agent,
            cookie_header=self.cookie_header,
        )

    @staticmethod
    def _is_rate_limited_payload(payload: dict[str, Any]) -> bool:
        return is_rate_limited_payload(payload)

    def _clear_wbi_cache(self) -> None:
        self._wbi_cache = None

    async def _request_with_transient_retry(
        self,
        request_coro,
        *,
        method: str,
        url: str,
    ) -> httpx.Response:
        return await request_with_transient_retry(
            request_coro,
            method=method,
            url=url,
            logger=LOGGER,
        )

    async def _signed_get_json(
        self,
        url: str,
        params: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = False,
        retry_on_wbi_miss: bool = True,
    ) -> dict[str, Any]:
        return await signed_get_json(
            http=self._http,
            sign_wbi=self.sign_wbi,
            clear_wbi_cache=self._clear_wbi_cache,
            logger=LOGGER,
            url=url,
            params=params,
            headers=headers,
            follow_redirects=follow_redirects,
            retry_on_wbi_miss=retry_on_wbi_miss,
        )

    async def _signed_post_json(
        self,
        url: str,
        params: dict[str, Any],
        body: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        retry_on_wbi_miss: bool = True,
    ) -> dict[str, Any]:
        return await signed_post_json(
            http=self._http,
            sign_wbi=self.sign_wbi,
            clear_wbi_cache=self._clear_wbi_cache,
            logger=LOGGER,
            url=url,
            params=params,
            body=body,
            headers=headers,
            retry_on_wbi_miss=retry_on_wbi_miss,
        )

    async def _signed_post_query_json(
        self,
        url: str,
        params: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = False,
        retry_on_wbi_miss: bool = True,
    ) -> dict[str, Any]:
        return await signed_post_query_json(
            http=self._http,
            sign_wbi=self.sign_wbi,
            clear_wbi_cache=self._clear_wbi_cache,
            logger=LOGGER,
            url=url,
            params=params,
            headers=headers,
            follow_redirects=follow_redirects,
            retry_on_wbi_miss=retry_on_wbi_miss,
        )

    async def _signed_post_form_json(
        self,
        url: str,
        params: dict[str, Any],
        body: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        retry_on_wbi_miss: bool = True,
    ) -> dict[str, Any]:
        return await signed_post_form_json(
            http=self._http,
            sign_wbi=self.sign_wbi,
            clear_wbi_cache=self._clear_wbi_cache,
            logger=LOGGER,
            url=url,
            params=params,
            body=body,
            headers=headers,
            retry_on_wbi_miss=retry_on_wbi_miss,
        )

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
        return parse_danmu_server_conf(payload, room_id)

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
        return parse_live_room_info(payload, room_id)

    async def get_room_owner_uid(self, room_id: int) -> int:
        response = await self._request_with_transient_retry(
            lambda: self._http.get(
                "https://api.live.bilibili.com/room/v1/Room/get_info",
                params={"room_id": room_id},
                headers=self._live_headers(room_id),
            ),
            method="GET",
            url="https://api.live.bilibili.com/room/v1/Room/get_info",
        )
        response.raise_for_status()
        return parse_room_owner_uid(response.json(), room_id)

    async def get_live_watch_time(
        self,
        room_id: int,
        *,
        ruid: int | None = None,
    ) -> LiveWatchTime:
        resolved_ruid = ruid or await self.get_room_owner_uid(room_id)
        response = await self._request_with_transient_retry(
            lambda: self._http.get(
                "https://api.live.bilibili.com/xlive/general-interface/v1/guard/GuardActive",
                params={"ruid": resolved_ruid, "platform": "pc"},
                headers=self._live_headers(room_id),
            ),
            method="GET",
            url="https://api.live.bilibili.com/xlive/general-interface/v1/guard/GuardActive",
        )
        response.raise_for_status()
        watch_time, rusername = parse_guard_active_watch_time(
            response.json(),
            resolved_ruid,
        )
        return LiveWatchTime(
            room_id=room_id,
            ruid=resolved_ruid,
            watch_time=watch_time,
            rusername=rusername,
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
        return hmac_by_rule(data, secret_key, rule)

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
        return build_x25kn_signature(
            live_buvid=self.live_buvid,
            live_uuid=self.live_uuid,
            parent_area_id=parent_area_id,
            area_id=area_id,
            seq_id=seq_id,
            room_id=room_id,
            ets=ets,
            duration=duration,
            ts_ms=ts_ms,
            secret_key=secret_key,
            secret_rule=secret_rule,
        )

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

        params = build_live_trace_enter_params(
            room,
            live_buvid=self.live_buvid,
            live_uuid=self.live_uuid,
            user_agent=self.user_agent,
            csrf=self.bili_jct,
            ts_ms=int(time.time() * 1000),
        )
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
        return live_trace_session_from_enter_payload(payload, room=room)

    async def live_trace_heartbeat(self, session: LiveTraceSession) -> LiveTraceSession:
        if not self.bili_jct:
            raise ValueError("cookie 缺少 bili_jct，无法发送 x25Kn/X")
        if not session.secret_key or not session.secret_rule:
            raise ValueError("x25Kn 会话缺少 secret_key/secret_rule")

        next_seq = session.seq_id + 1
        expected_interval = max(1, int(session.heartbeat_interval))
        duration_used = expected_interval
        ts_ms = int(time.time() * 1000)
        actual_elapsed = max(1, int(ts_ms / 1000 - session.ets))
        # 回退到老版本语义：固定心跳间隔上报，避免双路径重试放大抖动。
        # actual_elapsed 仅用于失败日志定位。

        signature = self._build_x25kn_signature(
            parent_area_id=session.parent_area_id,
            area_id=session.area_id,
            seq_id=next_seq,
            room_id=session.room_id,
            ets=session.ets,
            duration=duration_used,
            ts_ms=ts_ms,
            secret_key=session.secret_key,
            secret_rule=session.secret_rule,
        )

        params = build_live_trace_heartbeat_params(
            session,
            live_buvid=self.live_buvid,
            live_uuid=self.live_uuid,
            user_agent=self.user_agent,
            csrf=self.bili_jct,
            signature=signature,
            next_seq=next_seq,
            duration=duration_used,
            ts_ms=ts_ms,
        )

        payload = await self._signed_post_query_json(
            "https://live-trace.bilibili.com/xlive/data-interface/v1/x25Kn/X",
            params,
            headers=self._live_headers(session.room_id, lite=True),
            follow_redirects=True,
            retry_on_wbi_miss=True,
        )

        if payload.get("code") != 0:
            raise ValueError(
                "x25Kn/X 失败 "
                f"room_id={session.room_id}: {payload.get('message')} "
                f"(duration={duration_used}, expected={expected_interval}, elapsed={actual_elapsed})"
            )

        return apply_live_trace_heartbeat_payload(
            session,
            payload,
            next_seq=next_seq,
        )

    async def get_task_progress(self, task_ids: list[str]) -> list[TaskProgress]:
        normalized_ids = normalize_task_ids(task_ids)
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
        return parse_task_progress_payload(payload)

    async def get_mission_reward_info(self, task_id: str) -> MissionRewardInfo:
        normalized_id = normalize_task_id(task_id)

        payload: dict[str, Any] = {}
        for attempt, delay in enumerate(MISSION_RETRY_DELAYS, start=1):
            if delay > 0:
                await asyncio.sleep(delay)
            payload = await self._signed_get_json(
                "https://api.bilibili.com/x/activity_components/mission/info",
                {"task_id": normalized_id},
                headers=self._mission_headers(normalized_id),
                retry_on_wbi_miss=True,
            )
            if payload.get("code") == 0 or not self._is_rate_limited_payload(payload):
                break
            LOGGER.info(
                "查询领奖信息触发限频 task_id=%s attempt=%s，稍后重试",
                normalized_id,
                attempt,
            )
        return parse_mission_reward_info(payload, normalized_id=normalized_id)

    async def receive_mission_reward(
        self, info: MissionRewardInfo
    ) -> MissionRewardClaimResult:
        skipped_result = skipped_reward_claim_result(info)
        if skipped_result is not None:
            return skipped_result

        if not self.bili_jct:
            raise ValueError("cookie 缺少 bili_jct，无法领取奖励")

        body = build_reward_receive_body(info, csrf=self.bili_jct)
        payload: dict[str, Any] = {}
        for attempt, delay in enumerate(MISSION_RETRY_DELAYS, start=1):
            if delay > 0:
                await asyncio.sleep(delay)
            payload = await self._signed_post_form_json(
                "https://api.bilibili.com/x/activity_components/mission/receive",
                {},
                body,
                headers={
                    **self._mission_headers(info.task_id),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                retry_on_wbi_miss=True,
            )
            if payload.get("code") == 0 or not self._is_rate_limited_payload(payload):
                break
            LOGGER.info(
                "领取奖励触发限频 task_id=%s attempt=%s，稍后重试",
                info.task_id,
                attempt,
            )
        return reward_claim_result_from_payload(info, payload)

    async def receive_all_mission_rewards(
        self, task_ids: list[str]
    ) -> list[MissionRewardClaimResult]:
        normalized_ids = normalize_task_ids(task_ids)
        results: list[MissionRewardClaimResult] = []
        for index, task_id in enumerate(normalized_ids):
            try:
                info = await self.get_mission_reward_info(task_id)
                results.append(await self.receive_mission_reward(info))
            except Exception as exc:
                results.append(failed_reward_claim_result(task_id, exc))
            if index < len(normalized_ids) - 1:
                await asyncio.sleep(REWARD_CLAIM_INTERVAL_SECONDS)
        return results
