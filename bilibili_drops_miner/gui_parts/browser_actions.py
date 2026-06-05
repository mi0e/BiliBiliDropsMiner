from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtWidgets import QInputDialog, QMessageBox, QWidget

from bilibili_drops_miner.gui_parts.browser_sniffer import start_browser_sniff
from bilibili_drops_miner.gui_parts.browser_utils import (
    available_browsers,
    browser_label,
    browser_try_order,
    detect_default_browser,
    extract_room_id_from_live_url,
    find_browser,
)
from bilibili_drops_miner.utils import extract_bili_live_task_groups


class BrowserActions:
    def __init__(
        self,
        *,
        parent: QWidget | None,
        show_warning: Callable[[str, str], None],
        show_error: Callable[[str, str], None],
        post_ui_task: Callable[..., None],
        set_room_id: Callable[[int], None],
        set_cookie: Callable[[str], None],
        set_task_ids: Callable[[str], None],
        logger: logging.Logger | None = None,
    ) -> None:
        self._parent = parent
        self._show_warning = show_warning
        self._show_error = show_error
        self._post_ui_task = post_ui_task
        self._set_room_id = set_room_id
        self._set_cookie = set_cookie
        self._set_task_ids = set_task_ids
        self._logger = logger or logging.getLogger(__name__)

    @staticmethod
    def find_browser(name: str) -> bool:
        return find_browser(name)

    @staticmethod
    def detect_default_browser() -> str | None:
        return detect_default_browser()

    @staticmethod
    def available_browsers() -> list[str]:
        return available_browsers()

    @staticmethod
    def browser_label(browser: str) -> str:
        return browser_label(browser)

    @staticmethod
    def browser_try_order(preferred: str | None) -> tuple[str, ...]:
        return browser_try_order(preferred)

    @staticmethod
    def extract_room_id_from_live_url(text: str) -> int | None:
        return extract_room_id_from_live_url(text)

    def pick_browser(self) -> str | None:
        available = available_browsers()
        if not available:
            self._show_warning("提示", "未检测到 Chrome 或 Edge，请先安装浏览器。")
            return None
        if len(available) == 1:
            return available[0]

        default = detect_default_browser()
        if default not in available:
            default = available[0]

        msg = QMessageBox(self._parent)
        msg.setIcon(QMessageBox.Question)
        msg.setWindowTitle("选择浏览器")
        msg.setText(
            f"检测到系统默认浏览器为 {browser_label(default)}。\n"
            "请选择用于自动获取的浏览器："
        )

        default_btn = msg.addButton(
            f"默认 ({browser_label(default)})",
            QMessageBox.AcceptRole,
        )
        other_buttons: dict[object, str] = {}
        for browser in available:
            if browser == default:
                continue
            button = msg.addButton(
                browser_label(browser),
                QMessageBox.ActionRole,
            )
            other_buttons[button] = browser
        cancel_btn = msg.addButton("取消", QMessageBox.RejectRole)

        msg.exec()
        clicked = msg.clickedButton()
        if clicked is None or clicked == cancel_btn:
            return None
        if clicked == default_btn:
            return default
        return other_buttons.get(clicked, default)

    def apply_selected_task_group(
        self,
        room_id: int | None,
        task_groups: list[dict[str, object]],
    ) -> None:
        if room_id is not None and room_id > 0:
            self._set_room_id(room_id)

        if not task_groups:
            self._show_warning("提示", "未从当前直播页解析到掉宝任务分组")
            return

        options = [
            f"{str(group.get('label') or '任务组')} ({len(group.get('task_ids') or [])} 个任务)"
            for group in task_groups
        ]
        default_index = 0
        for index, group in enumerate(task_groups):
            if bool(group.get("active")):
                default_index = index
                break

        selected_option, ok = QInputDialog.getItem(
            self._parent,
            "选择掉宝任务组",
            "检测到多个按日期分组的掉宝任务，请选择要填入的一组：",
            options,
            default_index,
            False,
        )
        if not ok:
            self._logger.info("用户取消了掉宝任务组选择")
            return

        try:
            selected_index = options.index(selected_option)
        except ValueError:
            selected_index = default_index

        selected_group = task_groups[selected_index]
        task_ids = [
            str(task_id).strip()
            for task_id in (selected_group.get("task_ids") or [])
            if str(task_id).strip()
        ]
        if not task_ids:
            self._show_warning("提示", "所选分组中没有可用的任务 ID")
            return

        self._set_task_ids(",".join(task_ids))
        self._logger.info(
            "任务ID获取成功: %s -> %s",
            selected_group.get("label") or f"任务组 {selected_index + 1}",
            ",".join(task_ids),
        )

    def browser_sniff(
        self,
        url_keyword: str | None,
        hint: str,
        on_network_match: Callable[[Any], None] | None = None,
        on_cookies: Callable[[list[dict[str, Any]]], None] | None = None,
        on_page_url: Callable[[int], None] | None = None,
        on_page_html: Callable[[str, str], bool] | None = None,
        browser_preference: str | None = None,
        finish_on_any: bool = False,
    ) -> None:
        def on_error(title: str, message: str) -> None:
            self._post_ui_task(self._show_error, title, message)

        start_browser_sniff(
            url_keyword,
            hint,
            on_error=on_error,
            on_network_match=on_network_match,
            on_cookies=on_cookies,
            on_page_url=on_page_url,
            on_page_html=on_page_html,
            browser_preference=browser_preference,
            finish_on_any=finish_on_any,
            logger=self._logger,
        )

    def auto_fetch_room_id(self) -> None:
        ok = QMessageBox.question(
            self._parent,
            "无需登录，自动获取房间号",
            "支持 Chrome / Edge，将优先使用系统默认浏览器。<br><br>"
            "点击确定后选择浏览器，并在 2 分钟内进入目标直播间，<br>"
            "即可自动获取房间号。<br><br>"
            "捕获成功后浏览器会自动关闭。",
            QMessageBox.Ok | QMessageBox.Cancel,
        )
        if ok != QMessageBox.Ok:
            return

        browser = self.pick_browser()
        if browser is None:
            return

        def on_room(room_id: int) -> None:
            self._post_ui_task(self._set_room_id, room_id)
            self._logger.info("房间号获取成功: %s", room_id)

        self.browser_sniff(
            None,
            "已打开浏览器，请进入目标直播间",
            on_page_url=on_room,
            browser_preference=browser,
        )

    def auto_fetch_task_ids(self) -> None:
        ok = QMessageBox.question(
            self._parent,
            "无需登录，自动获取任务ID",
            "支持 Chrome / Edge，将优先使用系统默认浏览器。<br><br>"
            "点击确定后选择浏览器，并在 2 分钟内：<br><br>"
            "打开有当前任务的直播间即可自动获取任务ID和房间号，<br>"
            "或手动点击页面上的「刷新任务」按钮。<br><br>"
            "捕获成功后浏览器会自动关闭。",
            QMessageBox.Ok | QMessageBox.Cancel,
        )
        if ok != QMessageBox.Ok:
            return

        browser = self.pick_browser()
        if browser is None:
            return

        def on_page_html(page_html: str, page_url: str) -> bool:
            room_id = extract_room_id_from_live_url(page_url)
            task_groups = extract_bili_live_task_groups(page_html)
            if not task_groups:
                return False
            self._post_ui_task(self.apply_selected_task_group, room_id, task_groups)
            return True

        def on_match(payload: Any) -> None:
            payload_data = payload if isinstance(payload, dict) else {}
            request_url = str(payload_data.get("url") or "")
            page_url = str(payload_data.get("page_url") or "")
            room_id = extract_room_id_from_live_url(page_url)
            if room_id is None:
                room_id = extract_room_id_from_live_url(request_url)
            if room_id is not None:
                self._post_ui_task(self._set_room_id, room_id)
                self._logger.info("房间号获取成功: %s", room_id)

            data = payload_data.get("data")
            if not isinstance(data, dict):
                raise ValueError("task response payload invalid")
            if data.get("code") != 0:
                raise ValueError("response code != 0")
            tasks = data.get("data", {}).get("list", [])
            task_ids = [task.get("task_id") for task in tasks if task.get("task_id")]
            if not task_ids:
                raise ValueError("empty task list")
            self._post_ui_task(self._set_task_ids, ",".join(task_ids))
            self._logger.info(
                "任务ID获取成功（API 兜底）: %s",
                ",".join(task_ids),
            )

        self.browser_sniff(
            "/x/task/totalv2",
            "已打开浏览器，请打开有当前任务的直播间并等待页面加载完成",
            on_network_match=on_match,
            on_page_html=on_page_html,
            browser_preference=browser,
            finish_on_any=True,
        )

    def auto_fetch_cookie(self) -> None:
        ok = QMessageBox.question(
            self._parent,
            "自动获取Cookie",
            "支持 Chrome / Edge，将优先使用系统默认浏览器。<br><br>"
            "点击确定后选择浏览器，并在 2 分钟内登录 B 站，<br>"
            "进入任意页面后即可自动获取 Cookie。<br><br>"
            "捕获成功后浏览器会自动关闭。",
            QMessageBox.Ok | QMessageBox.Cancel,
        )
        if ok != QMessageBox.Ok:
            return

        browser = self.pick_browser()
        if browser is None:
            return

        def on_cookies(cookies: list[dict[str, Any]]) -> None:
            cookie_str = "; ".join(
                f"{cookie['name']}={cookie['value']}"
                for cookie in cookies
                if cookie.get("name")
            )
            if not cookie_str:
                self._post_ui_task(
                    self._show_warning,
                    "提示",
                    "未获取到 Cookie，请确认已登录 B 站",
                )
                return
            self._post_ui_task(self._set_cookie, cookie_str)
            self._logger.info("Cookie 获取成功")

        self.browser_sniff(
            None,
            "已打开浏览器，正在获取 Cookie…",
            on_cookies=on_cookies,
            browser_preference=browser,
        )
