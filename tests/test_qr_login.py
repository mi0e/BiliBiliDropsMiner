from __future__ import annotations

import os
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QWidget

from bilibili_drops_miner.client_parts.qr_login import (
    QrLoginApi,
    QrLoginChallenge,
    QrLoginError,
    QrLoginStatus,
    QrPollResult,
    build_login_cookie_string,
    parse_generate_payload,
    parse_poll_payload,
)
from bilibili_drops_miner.gui_parts.qr_login_dialog import (
    QrLoginDialog,
    make_qr_matrix,
    matrix_to_pixmap,
)


class QrLoginApiTests(unittest.TestCase):
    def test_generate_payload_requires_success_and_expected_fields(self) -> None:
        challenge = parse_generate_payload(
            {
                "code": 0,
                "data": {
                    "url": "https://passport.example.invalid/qr",
                    "qrcode_key": "temporary-key",
                },
            }
        )
        self.assertEqual(challenge.key, "temporary-key")

        invalid_payloads = (
            {"code": -1, "data": {}},
            {"code": False, "data": {}},
            {"code": 0, "data": None},
            {"code": 0, "data": {"url": "http://insecure.invalid", "qrcode_key": "k"}},
            {"code": 0, "data": {"url": "https://example.invalid", "qrcode_key": ""}},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(QrLoginError):
                parse_generate_payload(payload)

    def test_poll_payload_maps_all_documented_states_and_rejects_unknown(self) -> None:
        expected = {
            86101: QrLoginStatus.UNSCANNED,
            86090: QrLoginStatus.CONFIRMED_PENDING,
            86038: QrLoginStatus.EXPIRED,
            0: QrLoginStatus.SUCCESS,
        }
        for code, status in expected.items():
            with self.subTest(code=code):
                self.assertIs(
                    parse_poll_payload({"code": 0, "data": {"code": code}}),
                    status,
                )

        with self.assertRaisesRegex(QrLoginError, "未知扫码状态码"):
            parse_poll_payload({"code": 0, "data": {"code": 12345}})
        with self.assertRaises(QrLoginError):
            parse_poll_payload({"code": 0, "data": {"code": "0"}})

    def test_cookie_filter_uses_safe_names_and_requires_three_login_items(self) -> None:
        client_cookies = httpx.Cookies()
        client_cookies.set("buvid3", "device", domain=".bilibili.com")
        response_cookies = httpx.Cookies()
        response_cookies.set("SESSDATA", "session", domain=".bilibili.com")
        response_cookies.set("DedeUserID", "42", domain=".bilibili.com")
        response_cookies.set("bili_jct", "csrf", domain=".bilibili.com")
        response_cookies.set("refresh_token", "must-not-be-saved", domain=".bilibili.com")

        cookie = build_login_cookie_string(response_cookies, client_cookies)

        self.assertEqual(
            cookie,
            "SESSDATA=session; bili_jct=csrf; DedeUserID=42; buvid3=device",
        )
        self.assertNotIn("refresh_token", cookie)

        response_cookies.delete("bili_jct", domain=".bilibili.com")
        with self.assertRaisesRegex(QrLoginError, "Cookie 不完整"):
            build_login_cookie_string(response_cookies, client_cookies)

    def test_api_reuses_client_and_collects_success_response_cookies(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/generate"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "url": "https://passport.example.invalid/qr",
                            "qrcode_key": "temporary-key",
                        },
                    },
                )
            return httpx.Response(
                200,
                json={"code": 0, "data": {"code": 0}},
                headers=[
                    ("set-cookie", "SESSDATA=session; Domain=.bilibili.com; Path=/"),
                    ("set-cookie", "DedeUserID=42; Domain=.bilibili.com; Path=/"),
                    ("set-cookie", "bili_jct=csrf; Domain=.bilibili.com; Path=/"),
                    ("set-cookie", "refresh_token=ignored; Domain=.bilibili.com; Path=/"),
                ],
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        api = QrLoginApi(client)
        try:
            challenge = api.generate()
            result = api.poll(challenge.key)
        finally:
            api.close()

        self.assertIs(result.status, QrLoginStatus.SUCCESS)
        self.assertEqual(
            result.cookie,
            "SESSDATA=session; bili_jct=csrf; DedeUserID=42",
        )
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1].url.params["qrcode_key"], "temporary-key")


class _FakeSuccessApi:
    instances: list["_FakeSuccessApi"] = []

    def __init__(self) -> None:
        self.closed = threading.Event()
        self.__class__.instances.append(self)

    def generate(self) -> QrLoginChallenge:
        return QrLoginChallenge("https://passport.example.invalid/qr", "key")

    def poll(self, _key: str) -> QrPollResult:
        return QrPollResult(
            QrLoginStatus.SUCCESS,
            "SESSDATA=session; bili_jct=csrf; DedeUserID=42",
        )

    def close(self) -> None:
        self.closed.set()


class QrLoginDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _wait_until(self, predicate, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        return bool(predicate())

    def test_qr_is_generated_and_rendered_locally(self) -> None:
        matrix = make_qr_matrix("https://passport.example.invalid/qr")
        pixmap = matrix_to_pixmap(matrix, 240)

        self.assertGreater(len(matrix), 20)
        self.assertFalse(pixmap.isNull())
        image = pixmap.toImage()
        colors = {
            image.pixelColor(x, y).name()
            for x in range(image.width())
            for y in range(image.height())
        }
        self.assertIn(QColor("black").name(), colors)
        self.assertIn(QColor("white").name(), colors)

    def test_dialog_success_applies_cookie_after_client_is_closed(self) -> None:
        _FakeSuccessApi.instances.clear()
        applied: list[str] = []
        dialog = QrLoginDialog(
            None,
            applied.append,
            api_factory=_FakeSuccessApi,
            poll_interval=0.001,
            max_duration=1.0,
        )
        self.assertTrue(dialog.testAttribute(Qt.WA_DeleteOnClose))
        dialog.show()
        dialog.start_session()

        self.assertTrue(self._wait_until(lambda: bool(applied)))
        self.assertTrue(_FakeSuccessApi.instances[0].closed.is_set())
        self.assertEqual(len(applied), 1)

    def test_cancel_stops_poll_wait_and_closes_client_without_callback(self) -> None:
        _FakeSuccessApi.instances.clear()
        applied: list[str] = []
        dialog = QrLoginDialog(
            None,
            applied.append,
            api_factory=_FakeSuccessApi,
            poll_interval=30.0,
            max_duration=60.0,
        )
        dialog.show()
        dialog.start_session()
        self.assertTrue(self._wait_until(lambda: bool(_FakeSuccessApi.instances)))

        dialog.reject()

        self.assertTrue(
            self._wait_until(lambda: _FakeSuccessApi.instances[0].closed.is_set())
        )
        self.assertEqual(applied, [])

    def test_repeated_close_deletes_dialogs_from_parent(self) -> None:
        _FakeSuccessApi.instances.clear()
        parent = QWidget()

        for index in range(3):
            dialog = QrLoginDialog(
                parent,
                lambda _cookie: None,
                api_factory=_FakeSuccessApi,
                poll_interval=30.0,
                max_duration=60.0,
            )
            self.assertTrue(dialog.testAttribute(Qt.WA_DeleteOnClose))
            dialog.show()
            dialog.start_session()
            self.assertTrue(
                self._wait_until(lambda: len(_FakeSuccessApi.instances) > index)
            )

            dialog.reject()

            self.assertTrue(
                self._wait_until(
                    lambda: _FakeSuccessApi.instances[index].closed.is_set()
                )
            )
            QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.app.processEvents()
            self.assertEqual(parent.findChildren(QrLoginDialog), [])

        parent.close()


if __name__ == "__main__":
    unittest.main()
