from __future__ import annotations

import json
import logging
import unittest
from unittest.mock import AsyncMock, call, patch

import httpx

from bilibili_drops_miner.client_parts.core import BilibiliClient
from bilibili_drops_miner.client_parts.http import signed_request_json
from bilibili_drops_miner.client_parts.models import (
    MissionRewardClaimResult,
    MissionRewardInfo,
    TaskCheckpointProgress,
    TaskProgress,
)
from bilibili_drops_miner.client_parts.rewards import (
    REWARD_CLAIM_INTERVAL_SECONDS,
    parse_mission_reward_info,
)
from bilibili_drops_miner.client_parts.tasks import (
    parse_task_progress_payload,
    resolve_reward_task_ids,
)


class MissionRewardInfoParsingTest(unittest.TestCase):
    def _parse(self, data: dict) -> MissionRewardInfo:
        return parse_mission_reward_info({"code": 0, "data": data}, normalized_id="t1")

    def test_missing_or_invalid_status_is_not_claimable(self) -> None:
        for data in ({}, {"status": None}, {"status": ""}, {"status": "abc"}):
            with self.subTest(data=data):
                info = self._parse(data)
                self.assertFalse(info.is_claimable)
                self.assertFalse(info.is_claimed)

    def test_explicit_status_values_are_preserved(self) -> None:
        self.assertTrue(self._parse({"status": 0}).is_claimable)
        self.assertTrue(self._parse({"status": "0"}).is_claimable)
        self.assertTrue(self._parse({"status": 6}).is_claimed)


def _checkpoint(sid: str) -> TaskCheckpointProgress:
    return TaskCheckpointProgress(
        sid=sid,
        alias=sid,
        status=3,
        cur_value=1,
        limit_value=1,
    )


def _progress(
    task_id: str,
    *checkpoint_ids: str,
) -> TaskProgress:
    return TaskProgress(
        task_id=task_id,
        task_name=task_id,
        status=3,
        cur_value=1,
        limit_value=1,
        check_points=[_checkpoint(sid) for sid in checkpoint_ids],
    )


def _reward_info(task_id: str) -> MissionRewardInfo:
    return MissionRewardInfo(
        task_id=task_id,
        task_name=task_id,
        status=0,
        message="",
        act_id="activity",
        act_name="activity",
        reward_name="reward",
    )


def _claim_result(task_id: str) -> MissionRewardClaimResult:
    return MissionRewardClaimResult(
        task_id=task_id,
        task_name=task_id,
        reward_name="reward",
        status=6,
        message="领取成功",
        success=True,
        skipped=False,
        code=0,
    )


class RewardTaskResolutionTest(unittest.TestCase):
    def test_resolve_reward_task_ids_from_totalv2_checkpoints(self) -> None:
        progresses = parse_task_progress_payload(
            {
                "code": 0,
                "data": {
                    "list": [
                        {
                            "task_id": "parent",
                            "check_points": [
                                {"sid": "child-a", "alias": "奖励一"},
                                {"sid": "child-b", "alias": "奖励二"},
                            ],
                        }
                    ]
                },
            }
        )

        self.assertEqual(
            resolve_reward_task_ids(["parent"], progresses),
            ["child-a", "child-b"],
        )

    def test_resolve_reward_task_ids_expands_and_deduplicates_checkpoints(self) -> None:
        progresses = [
            _progress("parent", "child-a", "", "child-b", "child-a"),
            _progress("flat"),
        ]

        self.assertEqual(
            resolve_reward_task_ids(
                [" parent ", "flat", "missing", "flat"],
                progresses,
            ),
            ["child-a", "child-b", "flat", "missing"],
        )


class _FakeClaimClient:
    def __init__(
        self,
        progresses: list[TaskProgress],
        *,
        progress_error: Exception | None = None,
    ) -> None:
        self.progresses = progresses
        self.progress_error = progress_error
        self.progress_requests: list[list[str]] = []
        self.info_requests: list[str] = []
        self.receive_requests: list[str] = []

    async def get_task_progress(self, task_ids: list[str]) -> list[TaskProgress]:
        self.progress_requests.append(task_ids)
        if self.progress_error is not None:
            raise self.progress_error
        return self.progresses

    async def get_mission_reward_info(self, task_id: str) -> MissionRewardInfo:
        self.info_requests.append(task_id)
        return _reward_info(task_id)

    async def receive_mission_reward(
        self,
        info: MissionRewardInfo,
    ) -> MissionRewardClaimResult:
        self.receive_requests.append(info.task_id)
        return _claim_result(info.task_id)


class RewardClaimFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_receive_all_claims_checkpoint_ids_instead_of_parent(self) -> None:
        client = _FakeClaimClient([_progress("parent", "child-a", "child-b")])

        with patch(
            "bilibili_drops_miner.client_parts.core.asyncio.sleep",
            new_callable=AsyncMock,
        ) as sleep_mock:
            results = await BilibiliClient.receive_all_mission_rewards(
                client,  # type: ignore[arg-type]
                ["parent"],
            )

        self.assertEqual(client.progress_requests, [["parent"]])
        self.assertEqual(client.info_requests, ["child-a", "child-b"])
        self.assertEqual(client.receive_requests, ["child-a", "child-b"])
        self.assertEqual([result.task_id for result in results], ["child-a", "child-b"])
        sleep_mock.assert_awaited_once_with(REWARD_CLAIM_INTERVAL_SECONDS)

    async def test_receive_all_falls_back_to_parent_when_progress_query_fails(self) -> None:
        client = _FakeClaimClient(
            [],
            progress_error=ValueError("progress unavailable"),
        )

        with self.assertLogs(
            "bilibili_drops_miner.client_parts.core",
            level="WARNING",
        ) as captured:
            results = await BilibiliClient.receive_all_mission_rewards(
                client,  # type: ignore[arg-type]
                ["parent"],
            )

        self.assertEqual(client.info_requests, ["parent"])
        self.assertEqual([result.task_id for result in results], ["parent"])
        self.assertIn("将使用原任务 ID", captured.output[0])


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class _FakeHttp:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.get_calls = 0
        self.post_calls = 0

    async def request(self, method: str, *args, **kwargs) -> _FakeResponse:
        if method == "GET":
            self.get_calls += 1
        else:
            self.post_calls += 1
        return _FakeResponse(self.payload)


class SignedRequestRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_request_wire_format_for_query_form_and_json(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"code": 0})

        async def sign_wbi(params: dict) -> dict:
            return {**params, "w_rid": "sig"}

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            common = dict(
                http=http,
                sign_wbi=sign_wbi,
                clear_wbi_cache=lambda: None,
                logger=logging.getLogger(__name__),
                url="https://example.invalid/api",
                params={"csrf": "c"},
            )
            await signed_request_json(method="GET", **common)
            await signed_request_json(method="POST", data={"task_id": "t"}, **common)
            await signed_request_json(method="POST", json={"room_id": 1}, **common)

        self.assertEqual([r.method for r in requests], ["GET", "POST", "POST"])
        for request in requests:
            self.assertEqual(dict(request.url.params), {"csrf": "c", "w_rid": "sig"})
        self.assertEqual(requests[0].content, b"")
        self.assertEqual(requests[1].content, b"task_id=t")
        self.assertEqual(
            requests[1].headers["Content-Type"], "application/x-www-form-urlencoded"
        )
        self.assertEqual(json.loads(requests[2].content), {"room_id": 1})

    async def test_rate_limit_does_not_trigger_immediate_wbi_retry(self) -> None:
        http = _FakeHttp({"code": -509, "message": "请求过于频繁"})
        clear_calls = 0

        async def sign_wbi(params: dict) -> dict:
            return params

        def clear_wbi_cache() -> None:
            nonlocal clear_calls
            clear_calls += 1

        payload = await signed_request_json(
            http=http,  # type: ignore[arg-type]
            sign_wbi=sign_wbi,
            clear_wbi_cache=clear_wbi_cache,
            logger=logging.getLogger(__name__),
            method="GET",
            url="https://example.invalid",
            params={},
        )

        self.assertEqual(payload["code"], -509)
        self.assertEqual(http.get_calls, 1)
        self.assertEqual(clear_calls, 0)

    async def test_non_rate_limit_error_keeps_single_wbi_refresh_retry(self) -> None:
        http = _FakeHttp({"code": -403, "message": "invalid signature"})
        clear_calls = 0

        async def sign_wbi(params: dict) -> dict:
            return params

        def clear_wbi_cache() -> None:
            nonlocal clear_calls
            clear_calls += 1

        await signed_request_json(
            http=http,  # type: ignore[arg-type]
            sign_wbi=sign_wbi,
            clear_wbi_cache=clear_wbi_cache,
            logger=logging.getLogger(__name__),
            method="GET",
            url="https://example.invalid",
            params={},
        )

        self.assertEqual(http.get_calls, 2)
        self.assertEqual(clear_calls, 1)

    async def test_receive_rate_limit_does_not_trigger_immediate_wbi_retry(self) -> None:
        http = _FakeHttp({"code": -509, "message": "请求过于频繁"})
        clear_calls = 0

        async def sign_wbi(params: dict) -> dict:
            return params

        def clear_wbi_cache() -> None:
            nonlocal clear_calls
            clear_calls += 1

        payload = await signed_request_json(
            http=http,  # type: ignore[arg-type]
            sign_wbi=sign_wbi,
            clear_wbi_cache=clear_wbi_cache,
            logger=logging.getLogger(__name__),
            method="POST",
            url="https://example.invalid",
            params={},
            data={},
        )

        self.assertEqual(payload["code"], -509)
        self.assertEqual(http.post_calls, 1)
        self.assertEqual(clear_calls, 0)


class _RateLimitedMissionClient:
    def __init__(self) -> None:
        self.calls = 0

    @staticmethod
    def _mission_headers(task_id: str) -> dict[str, str]:
        return {}

    async def _signed_json(self, *args, **kwargs) -> dict:
        self.calls += 1
        return {"code": -509, "message": "请求过于频繁"}

    @staticmethod
    def _is_rate_limited_payload(payload: dict) -> bool:
        return True


class MissionRetryLogTest(unittest.IsolatedAsyncioTestCase):
    async def test_final_rate_limit_attempt_stops_instead_of_promising_retry(self) -> None:
        client = _RateLimitedMissionClient()

        with patch(
            "bilibili_drops_miner.client_parts.core.asyncio.sleep",
            new_callable=AsyncMock,
        ) as sleep_mock, self.assertLogs(
            "bilibili_drops_miner.client_parts.core",
            level="INFO",
        ) as captured:
            with self.assertRaisesRegex(ValueError, "请求过于频繁"):
                await BilibiliClient.get_mission_reward_info(
                    client,  # type: ignore[arg-type]
                    "child-a",
                )

        self.assertEqual(client.calls, 3)
        self.assertEqual(sleep_mock.await_args_list, [call(1.5), call(3.0)])
        self.assertIn("attempt=1/3，稍后重试", captured.output[0])
        self.assertIn("attempt=2/3，稍后重试", captured.output[1])
        self.assertIn("attempt=3/3，停止重试", captured.output[2])


if __name__ == "__main__":
    unittest.main()
