from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

try:
    from fastapi.testclient import TestClient
except ImportError:
    raise unittest.SkipTest("WebUI 测试需要 requirements-web.txt") from None

from bilibili_drops_miner.client_parts.models import MissionRewardClaimResult, TaskProgress
from bilibili_drops_miner.client_parts.qr_login import QrLoginChallenge, QrLoginStatus, QrPollResult
from bilibili_drops_miner.web import WebState, create_app


PASSWORD = "web-test-password-only"
COOKIE = "SESSDATA=test-secret; bili_jct=test-csrf; DedeUserID=123"


class FakeQr:
    instances = []

    def __init__(self):
        self.closed = False
        self.instances.append(self)

    def generate(self):
        return QrLoginChallenge("https://example.org/login", "private-key")

    def poll(self, key):
        return QrPollResult(QrLoginStatus.SUCCESS, COOKIE)

    def close(self):
        self.closed = True


class FakeMiner:
    """Delay resetting stop to reproduce the startup/stop race in miner.run."""
    active_session_count = 0
    def __init__(self, config):
        self.config = config
        self.uid = None
        self.login_invalidated = False
        self.stopped = threading.Event()

    def run(self):
        time.sleep(0.05)
        self.stopped.clear()
        self.uid = 123
        self.stopped.wait(2)

    def stop(self):
        self.stopped.set()


class WebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = create_app(data_dir=Path(self.directory.name), password=PASSWORD)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.auth = ("admin", PASSWORD)
        self.client.headers["X-Web-Request"] = "1"

    def post(self, path, body=None):
        return self.client.post("/api/" + path, json={} if body is None else body)

    def login(self):
        with patch("bilibili_drops_miner.web.QrLoginApi", FakeQr):
            response = self.post("qr")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("<svg", data["svg"])
            self.assertNotIn("private-key", response.text)
            response = self.post(f"qr/{data['id']}/poll")
            self.assertEqual(response.json(), {"status": "SUCCESS"})
            self.assertNotIn("test-secret", response.text)

    def test_auth_required_on_page_assets_and_api(self):
        self.client.auth = None
        for path in ["/", "/assets/app.js", "/api/state"]:
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.client.auth = ("admin", "wrong")
        self.assertEqual(self.post("start").status_code, 401)

    def test_csrf_header_required(self):
        del self.client.headers["X-Web-Request"]
        self.assertEqual(self.post("logout").status_code, 403)

    def test_default_without_password_allows_access_but_keeps_request_guard(self):
        with patch.dict(os.environ, {}, clear=True):
            app = create_app(data_dir=Path(self.directory.name))
        with TestClient(app) as client:
            for path in ["/", "/assets/app.js", "/api/state"]:
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("www-authenticate", response.headers)
            self.assertEqual(client.post("/api/stop").status_code, 403)
            self.assertEqual(client.post("/api/stop", headers={"X-Web-Request": "1"}).status_code, 200)

    def test_explicit_empty_password_overrides_environment(self):
        with patch.dict(os.environ, {"WEB_PASSWORD": PASSWORD}):
            app = create_app(data_dir=Path(self.directory.name), password="")
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/state").status_code, 200)

    def test_short_unicode_and_special_passwords_require_exact_match(self):
        for password in ["1", "密码", " a:b!@#$ "]:
            with self.subTest(password=password):
                with patch.dict(os.environ, {"WEB_PASSWORD": password}):
                    app = create_app(data_dir=Path(self.directory.name))
                with TestClient(app) as client:
                    self.assertEqual(client.get("/api/state").status_code, 401)
                    self.assertEqual(client.get("/api/state", auth=("admin", password)).status_code, 200)
                    self.assertEqual(client.get("/api/state", auth=("admin", password + "wrong")).status_code, 401)
                    self.assertEqual(client.get("/api/state", auth=("wrong", password)).status_code, 401)
                    self.assertEqual(client.get("/api/state", headers={"Authorization": "Basic ???"}).status_code, 401)

    def test_settings_do_not_accept_account_or_task_fields(self):
        for body in [{"cookie": COOKIE}, {"task_ids": ["injected"]}, {"room_ids": [True]}]:
            response = self.post("settings", body)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("test-secret", response.text)

    def test_settings_validation_and_room_changes_clear_groups(self):
        for rooms in [[0], [-1], [1, 1]]:
            self.assertEqual(self.post("settings", {"room_ids": rooms}).status_code, 400)
        self.post("settings", {"room_ids": [123]})
        self.app.state.web.groups = [{"task_ids": ["old"]}]
        self.app.state.web.selected = [0]
        response = self.post("settings", {"room_ids": [456]})
        self.assertEqual(response.json()["groups"], [])
        self.assertEqual(response.json()["selected"], [])

    def test_qr_credentials_persist_only_on_server_and_logout_clears(self):
        self.login()
        response = self.client.get("/api/state")
        self.assertTrue(response.json()["logged_in"])
        self.assertNotIn("test-secret", response.text)
        restored = WebState(Path(self.directory.name))
        self.assertEqual(restored.cookie, COOKIE)
        self.assertTrue(FakeQr.instances[-1].closed)
        self.assertEqual(self.post("logout").status_code, 200)
        self.assertEqual(WebState(Path(self.directory.name)).cookie, "")

    def test_qr_replaced_expired_and_shutdown_closed(self):
        with patch("bilibili_drops_miner.web.QrLoginApi", FakeQr):
            old = self.post("qr").json()["id"]
            first = FakeQr.instances[-1]
            new = self.post("qr").json()["id"]
            self.assertTrue(first.closed)
            self.assertEqual(self.post(f"qr/{old}/poll").status_code, 409)
            self.app.state.web.qr_deadline = 0
            self.assertEqual(self.post(f"qr/{new}/poll").json()["status"], "EXPIRED")
            self.assertTrue(FakeQr.instances[-1].closed)
            self.post("qr")
            self.app.state.web.shutdown()
            self.assertTrue(FakeQr.instances[-1].closed)

    def test_discovery_selection_accepts_only_server_groups(self):
        self.post("settings", {"room_ids": [123]})
        groups = [{"label": "今日", "active": True, "task_ids": ["a", "b"]}]
        with patch("bilibili_drops_miner.web.fetch_live_task_groups", return_value=groups) as fetch:
            data = self.post("discover").json()
            fetch.assert_called_once_with(123)
        self.assertEqual(data["selected"], [0])
        generation = data["generation"]
        self.assertEqual(self.post("selection", {"groups": [5], "generation": generation}).status_code, 400)
        self.assertEqual(self.post("selection", {"groups": [0], "generation": "stale"}).status_code, 409)
        self.assertEqual(self.post("selection", {"groups": [0], "generation": generation}).status_code, 200)
        self.assertEqual(self.app.state.web.task_ids(), ["a", "b"])

    def test_discovery_error_is_safe_and_releases_guard(self):
        self.post("settings", {"room_ids": [123]})
        with patch("bilibili_drops_miner.web.fetch_live_task_groups", side_effect=RuntimeError(COOKIE)):
            response = self.post("discover")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("test-secret", response.text)
        self.assertFalse(self.app.state.web.discovering)

    def test_empty_static_html_does_not_fallback(self):
        self.post("settings", {"room_ids": [123]})
        with patch("bilibili_drops_miner.web.fetch_live_task_groups", return_value=[]):
            response = self.post("discover")
        self.assertEqual(response.json()["groups"], [])

    def test_start_requires_login_and_rooms(self):
        self.assertEqual(self.post("start").status_code, 400)

    def test_manual_cookie_persists_without_being_returned(self):
        response = self.post("cookie", {"cookie": COOKIE})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("test-secret", response.text)
        self.assertEqual(WebState(Path(self.directory.name)).cookie, COOKIE)
        state = self.client.get("/api/state")
        self.assertTrue(state.json()["logged_in"])
        self.assertNotIn("test-secret", state.text)

    def test_invalid_manual_cookie_preserves_previous_login(self):
        self.login()
        for value in ["SESSDATA=test-secret", COOKIE + "\r\ninvalid"]:
            response = self.post("cookie", {"cookie": value})
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("test-secret", response.text)
            self.assertEqual(self.app.state.web.cookie, COOKIE)

    def test_manual_cookie_invalidates_pending_qr(self):
        with patch("bilibili_drops_miner.web.QrLoginApi", FakeQr):
            qr_id = self.post("qr").json()["id"]
            self.assertEqual(self.post("cookie", {"cookie": COOKIE}).status_code, 200)
            self.assertTrue(FakeQr.instances[-1].closed)
            self.assertEqual(self.post(f"qr/{qr_id}/poll").status_code, 409)

    def test_manual_ids_merge_deduplicate_persist_and_can_be_cleared(self):
        state = self.app.state.web
        state.groups = [{"task_ids": ["a", "group-task"]}]
        state.selected = [0]
        response = self.post("task-ids", {"task_ids": " a，b\na "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(state.task_ids(), ["a", "b", "group-task"])
        self.assertEqual(WebState(Path(self.directory.name)).task_ids(), state.task_ids())
        self.assertEqual(self.client.get("/api/state").json()["manual_task_ids"], ["a", "b"])
        self.post("task-ids", {"task_ids": ""})
        self.assertEqual(state.task_ids(), ["a", "group-task"])

    def test_old_state_without_manual_ids_loads(self):
        import json
        self.post("settings", {"room_ids": [123]})
        path = Path(self.directory.name) / "web-state.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.pop("manual_task_ids")
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(WebState(path.parent).manual_task_ids, [])

    def test_room_title_is_cached_and_client_closed(self):
        self.post("settings", {"room_ids": [123]})
        with patch("bilibili_drops_miner.web.BilibiliClient") as factory:
            client = factory.return_value
            client.get_room_title = AsyncMock(return_value="直播间名称")
            client.close = AsyncMock()
            for _ in range(2):
                response = self.client.get("/api/room/123")
                self.assertEqual(response.json(), {"room_id": 123, "title": "直播间名称"})
            client.get_room_title.assert_awaited_once_with(123)
            client.close.assert_awaited_once()
        self.assertEqual(self.client.get("/api/room/456").status_code, 404)

    def test_room_failure_falls_back_without_repeated_requests(self):
        self.post("settings", {"room_ids": [123]})
        with patch("bilibili_drops_miner.web.BilibiliClient") as factory:
            client = factory.return_value
            client.get_room_title = AsyncMock(side_effect=TimeoutError())
            client.close = AsyncMock()
            for _ in range(2):
                response = self.client.get("/api/room/123")
                self.assertEqual(response.json(), {"room_id": 123, "title": ""})
            client.get_room_title.assert_awaited_once()
            client.close.assert_awaited_once()

    def test_overview_reports_actual_workers_and_total_across_rooms(self):
        from types import SimpleNamespace
        response = self.post("settings", {"room_ids": [123], "thread_count": 32})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["planned_sessions"], 32)
        self.assertEqual(response.json()["active_sessions"], 0)
        self.app.state.web.miner = SimpleNamespace(active_session_count=15)
        data = self.client.get("/api/state").json()
        self.assertEqual((data["active_sessions"], data["planned_sessions"]), (15, 32))
        self.app.state.web.miner = None
        response = self.post("settings", {"room_ids": [123, 456], "thread_count": 32})
        self.assertEqual(response.json()["planned_sessions"], 64)

    def test_immediate_stop_prevents_duplicate_start_and_releases_thread(self):
        self.login()
        self.post("settings", {"room_ids": [123]})
        with patch("bilibili_drops_miner.web.BilibiliWatchTimeMiner", FakeMiner):
            self.assertEqual(self.post("start").status_code, 200)
            self.assertEqual(self.post("start").status_code, 409)
            self.assertEqual(self.post("logout").status_code, 409)
            self.assertEqual(self.post("cookie", {"cookie": COOKIE}).status_code, 409)
            self.assertEqual(self.post("task-ids", {"task_ids": "test-task"}).status_code, 200)
            self.assertEqual(self.post("settings", {"room_ids": [456]}).status_code, 409)
            self.assertEqual(self.post("stop").status_code, 200)
            self.app.state.web.thread.join(1)
            self.assertFalse(self.app.state.web.thread.is_alive())
            self.assertEqual(self.client.get("/api/state").json()["phase"], "stopped")

    def test_shutdown_stops_active_miner(self):
        self.login()
        self.post("settings", {"room_ids": [123]})
        with patch("bilibili_drops_miner.web.BilibiliWatchTimeMiner", FakeMiner):
            self.post("start")
            self.app.state.web.shutdown()
            self.assertFalse(self.app.state.web.thread.is_alive())
            self.assertEqual(self.post("start").status_code, 409)

    def test_static_files_and_security_headers(self):
        for path in ["/", "/assets/app.js", "/assets/style.css"]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertEqual(self.client.get("/assets/web-state.json").status_code, 404)

    def prepare_tasks(self):
        self.login()
        self.app.state.web.groups = [{"task_ids": ["task-a"]}]
        self.app.state.web.selected = [0]

    def test_running_discovery_and_selection_update_live_monitor_without_restart(self):
        self.login()
        self.post("settings", {"room_ids": [123]})
        self.post("task-ids", {"task_ids": "manual"})
        with patch("bilibili_drops_miner.web.BilibiliWatchTimeMiner", FakeMiner):
            self.assertEqual(self.post("start").status_code, 200)
            state = self.app.state.web
            miner, thread = state.miner, state.thread
            groups = [{"label": "new", "active": True, "task_ids": ["new-task"]}]
            with patch("bilibili_drops_miner.web.fetch_live_task_groups", return_value=groups):
                response = self.post("discover")
            self.assertEqual(response.status_code, 200)
            self.assertIs(state.miner, miner)
            self.assertIs(state.thread, thread)
            self.assertTrue(thread.is_alive())
            self.assertEqual(miner.config.task_ids, ["manual", "new-task"])
            stale = TaskProgress("manual", "old", 1, 1, 10)
            miner.on_task_progress([stale], ["manual"])
            self.assertEqual(state.snapshot()["task_progress"], [])
            fresh = TaskProgress("new-task", "new", 1, 2, 10)
            miner.on_task_progress([fresh], ["manual", "new-task"])
            self.assertEqual(state.snapshot()["task_progress"][0]["cur_value"], 2)
            self.assertEqual(self.post("selection", {"groups": [], "generation": state.generation}).status_code, 200)
            self.assertEqual(miner.config.task_ids, ["manual"])
            self.assertEqual(state.snapshot()["task_progress"], [])
            self.assertEqual(self.post("task-ids", {"task_ids": "replacement"}).status_code, 200)
            self.assertEqual(miner.config.task_ids, ["replacement"])
            self.assertEqual(self.post("settings", {"room_ids": [456]}).status_code, 409)
            state.phase = "stopping"
            self.assertEqual(self.post("discover").status_code, 409)
            self.post("stop")
            thread.join(1)
            self.assertFalse(thread.is_alive())

    def test_discovery_blocks_other_task_changes_but_allows_stop(self):
        entered, release = threading.Event(), threading.Event()
        def fetch(_room):
            entered.set()
            release.wait(2)
            return []
        self.post("settings", {"room_ids": [123]})
        results = []
        with patch("bilibili_drops_miner.web.fetch_live_task_groups", side_effect=fetch):
            thread = threading.Thread(target=lambda: results.append(self.post("discover")))
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.post("discover").status_code, 409)
                self.assertEqual(self.post("task-ids", {"task_ids": "x"}).status_code, 409)
                self.assertEqual(self.post("stop").status_code, 200)
            finally:
                release.set()
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].status_code, 200)
        self.assertFalse(self.app.state.web.discovering)

    def test_background_progress_reaches_state_and_survives_stop(self):
        from bilibili_drops_miner.client_parts.models import TaskCheckpointProgress
        self.prepare_tasks()
        self.post("settings", {"room_ids": [123]})
        self.post("task-ids", {"task_ids": "task-a"})
        reported = threading.Event()
        progress = TaskProgress("task-a", "观看", 1, 2, 5,
                                [TaskCheckpointProgress("one", "时长", 1, 2, 5)])

        class ReportingMiner(FakeMiner):
            def run(inner):
                inner.on_task_progress([progress], ["task-a"])
                reported.set()
                super().run()

        with patch("bilibili_drops_miner.web.BilibiliWatchTimeMiner", ReportingMiner):
            self.assertEqual(self.post("start").status_code, 200)
            self.assertTrue(reported.wait(1))
            with patch("bilibili_drops_miner.web.BilibiliClient") as factory:
                for _ in range(2):
                    data = self.client.get("/api/state").json()
                    self.assertEqual(data["task_progress"][0]["check_points"][0]["cur_value"], 2)
                    self.assertIsNotNone(data["progress_version"])
                factory.assert_not_called()
            self.post("stop")
            self.app.state.web.thread.join(1)
            self.assertFalse(self.app.state.web.thread.is_alive())
            self.assertEqual(self.client.get("/api/state").json()["task_progress"], data["task_progress"])

    def test_progress_discards_old_account_or_selection_and_redacts(self):
        self.prepare_tasks()
        state = self.app.state.web
        context = state.task_context()
        state.publish_progress([{"task_id": "task-a", "task_name": COOKIE}], context)
        response = self.client.get("/api/state")
        self.assertNotIn("test-secret", response.text)
        self.assertTrue(response.json()["task_progress"])
        self.post("task-ids", {"task_ids": "new-task"})
        self.assertEqual(self.client.get("/api/state").json()["task_progress"], [])
        state.publish_progress([{"task_id": "task-a"}], context)
        self.assertEqual(state.snapshot()["task_progress"], [])
        context = state.task_context()
        state.publish_progress([], context)
        self.assertIsNotNone(state.snapshot()["progress_version"])
        self.post("logout")
        state.publish_progress([{"task_id": "task-a"}], context)
        self.assertIsNone(state.snapshot()["progress_version"])

    def test_task_progress_uses_selected_ids_and_closes_client(self):
        self.prepare_tasks()
        with patch("bilibili_drops_miner.web.BilibiliClient") as factory:
            client = factory.return_value
            client.get_task_progress = AsyncMock(return_value=[TaskProgress("task-a", "观看", 1, 2, 5)])
            client.close = AsyncMock()
            response = self.post("tasks/progress")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["items"][0]["cur_value"], 2)
            self.assertEqual(self.client.get("/api/state").json()["task_progress"][0]["cur_value"], 2)
            client.get_task_progress.assert_awaited_once_with(["task-a"])
            client.close.assert_awaited_once()
        self.assertFalse(self.app.state.web.task_busy)

    def test_reward_failure_does_not_expose_exception_url_or_cookie(self):
        self.prepare_tasks()
        with patch("bilibili_drops_miner.web.BilibiliClient") as factory:
            client = factory.return_value
            client.receive_all_mission_rewards = AsyncMock(return_value=[
                MissionRewardClaimResult("task-a", "task", "", -1,
                                         "https://example.org/?csrf=test-csrf " + COOKIE,
                                         False, False)
            ])
            client.close = AsyncMock()
            response = self.post("tasks/claim")
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["items"][0]["success"])
            self.assertNotIn("test-secret", response.text)
            self.assertNotIn("test-csrf", response.text)
            self.assertNotIn("example.org", response.text)
            client.close.assert_awaited_once()

    def test_task_failure_releases_guard_even_if_close_fails(self):
        self.prepare_tasks()
        with patch("bilibili_drops_miner.web.BilibiliClient") as factory:
            client = factory.return_value
            client.get_task_progress = AsyncMock(side_effect=RuntimeError(COOKIE))
            client.close = AsyncMock(side_effect=RuntimeError(COOKIE))
            response = self.post("tasks/progress")
            self.assertEqual(response.status_code, 500)
            self.assertNotIn("test-secret", response.text)
        self.assertFalse(self.app.state.web.task_busy)

    def test_task_client_construction_failure_releases_guard(self):
        self.prepare_tasks()
        with patch("bilibili_drops_miner.web.BilibiliClient", side_effect=RuntimeError(COOKIE)):
            self.assertEqual(self.post("tasks/progress").status_code, 502)
        self.assertFalse(self.app.state.web.task_busy)

    def test_short_cookie_values_do_not_corrupt_room_or_connection_numbers(self):
        from bilibili_drops_miner.web import redact
        cookie = COOKIE + "; live_status=1; preference=5"
        for number in (71, 72, 73, 74, 75, 76):
            line = f"直播间 229158869 连接 #{number} 已启动"
            self.assertEqual(redact(line, cookie), line)
        self.assertNotIn("test-secret", redact(COOKIE, cookie))

    def test_account_validation_distinguishes_invalid_and_network_error(self):
        self.assertEqual(self.client.get("/api/account").json()["status"], "empty")
        for outcome, expected in [((123, "测试账号"), "valid"), ((None, ""), "invalid"), (TimeoutError(), "error")]:
            with self.subTest(expected=expected):
                self.app.state.web.cookie = COOKIE + "; variant=" + expected
                with patch("bilibili_drops_miner.web.BilibiliClient") as factory:
                    client = factory.return_value
                    client.get_self_info = AsyncMock()
                    if isinstance(outcome, Exception):
                        client.get_self_info.side_effect = outcome
                    else:
                        client.get_self_info.return_value = outcome
                    client.close = AsyncMock()
                    for _ in range(2):
                        response = self.client.get("/api/account")
                        self.assertEqual(response.json()["status"], expected)
                        self.assertNotIn("test-secret", response.text)
                    if expected == "valid":
                        self.assertEqual(response.json()["name"], "测试账号")
                    client.get_self_info.assert_awaited_once()
                    client.close.assert_awaited_once()
        self.post("logout")
        self.assertEqual(self.client.get("/api/account").json()["status"], "empty")

    def test_logs_redact_credentials_and_request_urls(self):
        self.login()
        logging.getLogger("bilibili_drops_miner.client_parts.core").warning(
            "request failed: %s https://example.org/?csrf=test-csrf", COOKIE)
        response = self.client.get("/api/state")
        self.assertIn("request failed", response.text)
        self.assertNotIn("test-secret", response.text)
        self.assertNotIn("test-csrf", response.text)
        self.assertNotIn("example.org", response.text)


if __name__ == "__main__":
    unittest.main()
