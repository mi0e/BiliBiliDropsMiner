from __future__ import annotations

import hashlib
import json
import unittest
import urllib.parse

import httpx

from bilibili_drops_miner.client_parts.constants import WBI_MIXIN_KEY_ENC_TAB
from bilibili_drops_miner.client_parts.cookies import (
    DEFAULT_USER_AGENT,
    build_cookie_state,
    build_live_headers,
    build_mission_headers,
)
from bilibili_drops_miner.client_parts.models import MissionRewardInfo, TaskProgress
from bilibili_drops_miner.client_parts.profile import (
    parse_self_info,
    validate_nav_payload,
)
from bilibili_drops_miner.client_parts.task_parsing import (
    coerce_task_number,
    extract_task_indicator_values,
)
from bilibili_drops_miner.client_parts.task_discovery import fetch_live_task_groups
from bilibili_drops_miner.client_parts.wbi import (
    encode_query,
    parse_wbi_keys_from_nav,
    sign_wbi_params,
)


class ClientPartsTest(unittest.TestCase):
    def test_fetch_live_task_groups_follows_redirect_and_sets_headers(self) -> None:
        requests: list[httpx.Request] = []
        state = {
            "EraTasklistPc": [{"tasklist": [{"taskId": "task-a"}]}],
            "EvaPositionBox": [{"left": 0, "top": 0}],
            "EvaTabs.Panel": [
                {
                    "id": "today",
                    "tabItem": {"tabItemProps": {"textContent": {"content": "今天"}}},
                }
            ],
            "EvaTabs": [{"activatedTabPanelId": "today"}],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/23612045":
                return httpx.Response(302, headers={"Location": "/redirected"})
            return httpx.Response(
                200,
                text=f"<script>window.__initialState = {json.dumps(state)};</script>",
            )

        groups = fetch_live_task_groups(
            23612045,
            transport=httpx.MockTransport(handler),
        )

        self.assertEqual(
            [request.url.path for request in requests],
            ["/23612045", "/redirected"],
        )
        self.assertEqual(requests[0].headers["User-Agent"], DEFAULT_USER_AGENT)
        self.assertEqual(requests[0].headers["Referer"], "https://live.bilibili.com/")
        self.assertEqual(groups[0]["task_ids"], ["task-a"])

    def test_fetch_live_task_groups_returns_empty_static_result(self) -> None:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(200, text="<html>no tasks</html>")
        )
        self.assertEqual(fetch_live_task_groups(1, transport=transport), [])

    def test_fetch_live_task_groups_raises_for_http_error(self) -> None:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(503, text="upstream detail")
        )
        with self.assertRaises(httpx.HTTPStatusError):
            fetch_live_task_groups(1, transport=transport)

    def test_task_completion_and_reward_status(self) -> None:
        self.assertTrue(
            TaskProgress(
                task_id="task-a",
                task_name="watch",
                status=1,
                cur_value=60,
                limit_value=60,
            ).is_completed
        )
        self.assertFalse(
            TaskProgress(
                task_id="task-a",
                task_name="watch",
                status=1,
                cur_value=30,
                limit_value=60,
            ).is_completed
        )
        self.assertTrue(
            TaskProgress(
                task_id="task-a",
                task_name="watch",
                status=6,
                cur_value=0,
                limit_value=0,
            ).is_completed
        )

        claimable = MissionRewardInfo(
            task_id="task-a",
            task_name="watch",
            status=0,
            message="",
            act_id="act",
            act_name="activity",
            reward_name="reward",
        )
        claimed = MissionRewardInfo(
            task_id="task-a",
            task_name="watch",
            status=6,
            message="已领取",
            act_id="act",
            act_name="activity",
            reward_name="reward",
        )
        self.assertTrue(claimable.is_claimable)
        self.assertFalse(claimable.is_claimed)
        self.assertFalse(claimed.is_claimable)
        self.assertTrue(claimed.is_claimed)

    def test_task_indicator_parsing_prefers_watch_indicator(self) -> None:
        self.assertEqual(coerce_task_number("12"), 12)
        self.assertEqual(coerce_task_number("12.5"), 12.5)
        self.assertEqual(coerce_task_number("not-a-number"), 0)

        cur_value, limit_value = extract_task_indicator_values(
            [
                {"type": "coin", "cur_value": 9, "limit": 99},
                {"type": "watch_time", "cur_value": "30", "limit": "60"},
            ]
        )
        self.assertEqual((cur_value, limit_value), (30, 60))

    def test_wbi_key_parse_and_signature(self) -> None:
        img_key = "abcdefghijklmnopqrstuvwxyzabcdef"
        sub_key = "1234567890abcdef1234567890abcdef"
        payload = {
            "data": {
                "wbi_img": {
                    "img_url": f"https://i0.hdslb.com/bfs/wbi/{img_key}.png",
                    "sub_url": f"https://i0.hdslb.com/bfs/wbi/{sub_key}.png",
                }
            }
        }
        self.assertEqual(parse_wbi_keys_from_nav(payload), (img_key, sub_key))

        params = {"b": "2", "a": "x! y"}
        signed = sign_wbi_params(params, img_key=img_key, sub_key=sub_key, timestamp=123)

        raw_mixin = img_key + sub_key
        mixin_key = "".join(raw_mixin[index] for index in WBI_MIXIN_KEY_ENC_TAB)[:32]
        sorted_items = {"a": "x! y", "b": "2", "wts": 123}
        expected_query = "&".join(
            f"{urllib.parse.quote(str(key), safe='')}="
            f"{urllib.parse.quote(str(value).replace('!', ''), safe='')}"
            for key, value in sorted_items.items()
        )
        expected_rid = hashlib.md5(
            (expected_query + mixin_key).encode("utf-8")
        ).hexdigest()
        self.assertEqual(signed["wts"], 123)
        self.assertEqual(signed["w_rid"], expected_rid)
        self.assertEqual(encode_query({"a": "x!()*"}), "a=x")

    def test_profile_and_cookie_helpers(self) -> None:
        validate_nav_payload({"code": 0})
        validate_nav_payload({"code": -101})
        with self.assertRaisesRegex(ValueError, "登录状态异常"):
            validate_nav_payload({"code": -400, "message": "bad"})

        self.assertEqual(parse_self_info({"data": {"isLogin": False}}), (None, ""))
        self.assertEqual(
            parse_self_info({"data": {"isLogin": True, "mid": "42", "uname": "user"}}),
            (42, "user"),
        )
        self.assertEqual(
            parse_self_info({"data": {"isLogin": True, "mid": 0, "uname": "ghost"}}),
            (None, "ghost"),
        )

        cookie_map, cookie_header, bili_jct = build_cookie_state(
            "SESSDATA=x; bili_jct=csrf; DedeUserID=1",
            fallback_buvid3="existing",
        )
        self.assertEqual(cookie_map["buvid3"], "existing")
        self.assertEqual(bili_jct, "csrf")
        self.assertIn("bili_jct=csrf", cookie_header)
        self.assertEqual(
            build_live_headers(
                123,
                user_agent=DEFAULT_USER_AGENT,
                cookie_header=cookie_header,
                lite=True,
            )["Referer"],
            "https://live.bilibili.com/blanc/123?liteVersion=true",
        )
        self.assertTrue(
            build_mission_headers(
                "task id",
                user_agent=DEFAULT_USER_AGENT,
                cookie_header=cookie_header,
            )["Referer"].endswith("task_id=task%20id")
        )


if __name__ == "__main__":
    unittest.main()
