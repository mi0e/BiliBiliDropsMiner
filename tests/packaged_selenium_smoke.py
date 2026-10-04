"""Opt-in source/frozen GUI and Selenium smoke test using synthetic local data.

Run with a 120-second subprocess timeout. Browser profiles and Qt settings are
temporary; no real Bilibili account or user browser profile is used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if "__compiled__" not in globals():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("SE_AVOID_STATS", "true")
os.environ.setdefault("SE_AVOID_BROWSER_DOWNLOAD", "true")
os.environ.setdefault("SE_TIMEOUT", "30")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QPushButton
from selenium import webdriver
from selenium.webdriver.common.selenium_manager import SeleniumManager

from bilibili_drops_miner.gui_parts import browser_sniffer
from bilibili_drops_miner.gui_parts.app_style import configure_qt_app
from bilibili_drops_miner.gui_parts.gui_state import GuiStateStore
from bilibili_drops_miner.gui_parts.main_window import MinerGUI
from bilibili_drops_miner.gui_parts.qr_login_dialog import make_qr_matrix, matrix_to_pixmap


def check_gui(temp_dir: str) -> None:
    app = QApplication.instance() or QApplication([])
    configure_qt_app(app)
    settings = QSettings(str(Path(temp_dir) / "gui.ini"), QSettings.IniFormat)
    window = MinerGUI(gui_state=GuiStateStore(settings))
    try:
        window.show()
        app.processEvents()
        labels = {button.text() for button in window.findChildren(QPushButton)}
        assert {"自动获取", "自动获取模式1", "自动获取模式2", "启动", "停止"} <= labels
        assert not matrix_to_pixmap(make_qr_matrix("https://example.invalid/test")).isNull()
    finally:
        window.close()
        app.processEvents()
    print("PASS GUI layout, QR rendering and clean close", flush=True)


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        if self.path.startswith("/x/task/totalv2"):
            body = json.dumps({"code": 0, "data": {"smoke": True}}).encode()
            self.send_header("Content-Type", "application/json")
        else:
            body = (
                '<html><body>packaged-smoke-fixture<script>'
                'setInterval(()=>fetch("/x/task/totalv2"),500);'
                '</script></body></html>'
            ).encode()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Set-Cookie", "SESSDATA=synthetic; Domain=.bilibili.com; Path=/")
            self.send_header("Set-Cookie", "DedeUserID=1; Domain=.bilibili.com; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


def check_browser(browser: str, temp_dir: str) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    # Preserve the production URL shape (no port); the browser alone maps it
    # to our loopback fixture, without modifying DNS or the system hosts file.
    url = "http://live.bilibili.com/12345"
    observations: set[str] = set()
    statuses: list[str] = []
    ready = threading.Event()
    finished = threading.Event()
    errors: list[str] = []
    drivers = []
    attribute = "Chrome" if browser == "chrome" else "Edge"
    original_driver = getattr(webdriver, attribute)
    original_order = browser_sniffer.browser_try_order

    def create_driver(*, options, service):
        options.add_argument("--headless=new")
        options.add_argument(f"--user-data-dir={Path(temp_dir) / browser}")
        options.add_argument("--no-proxy-server")
        options.add_argument(
            f"--host-resolver-rules=MAP live.bilibili.com 127.0.0.1:{server.server_port}"
        )
        options.add_argument("--disable-features=HttpsUpgrades")
        driver = original_driver(options=options, service=service)
        driver.set_page_load_timeout(20)
        drivers.append(driver)
        return driver

    def cookies(payload):
        assert {cookie["name"] for cookie in payload} >= {"SESSDATA", "DedeUserID"}
        observations.add("cookies")

    def network(payload):
        assert payload["data"] == {"code": 0, "data": {"smoke": True}}
        observations.add("network")

    def page(html, page_url):
        assert "packaged-smoke-fixture" in html and page_url == url
        observations.add("html")
        return True

    def room(room_id):
        assert room_id == 12345
        observations.add("room")

    setattr(webdriver, attribute, create_driver)
    browser_sniffer.browser_try_order = lambda _preference: (browser,)
    try:
        worker = browser_sniffer.start_browser_sniff(
            "/x/task/totalv2", "local smoke", start_url=url,
            on_error=lambda title, message: errors.append(f"{title}: {message}"),
            on_network_match=network, on_cookies=cookies,
            on_page_url=room, on_page_html=page, browser_preference=browser,
            on_preparation_status=statuses.append,
            on_browser_ready=ready.set, on_finished=finished.set,
        )
        worker.join(timeout=80)
        assert not worker.is_alive(), f"{browser}: sniffer timed out; got {sorted(observations)}"
        assert not errors, errors
        assert observations == {"cookies", "network", "html", "room"}, observations
        assert statuses and ready.is_set() and finished.is_set()
        assert drivers and all(d.service.process.poll() is not None for d in drivers)
    finally:
        setattr(webdriver, attribute, original_driver)
        browser_sniffer.browser_try_order = original_order
        for driver in drivers:
            driver.quit()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
    print(f"PASS {browser}: extension, cookies, network, page, room and driver cleanup", flush=True)
    print("Preparation stages: " + json.dumps(statuses, ensure_ascii=True), flush=True)


def main() -> None:
    manager = SeleniumManager._get_binary()
    result = subprocess.run([str(manager), "--version"], capture_output=True, timeout=10, check=True)
    print("PASS native Selenium Manager: " + result.stdout.decode().strip(), flush=True)
    with tempfile.TemporaryDirectory(prefix="bili-packaged-smoke-") as temp_dir:
        check_gui(temp_dir)
        for browser in sys.argv[1:] or ["chrome", "edge"]:
            if browser not in {"chrome", "edge"}:
                raise ValueError(f"Unknown browser: {browser}")
            check_browser(browser, temp_dir)


if __name__ == "__main__":
    main()
