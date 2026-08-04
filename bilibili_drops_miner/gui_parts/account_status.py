from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Callable, Literal

from PySide6.QtCore import QObject, QTimer, Qt, Signal

from bilibili_drops_miner.client import BilibiliClient


AccountStatusKind = Literal["empty", "checking", "valid", "invalid", "error"]
AccountChecker = Callable[[str], tuple[int | None, str]]


@dataclass(frozen=True, slots=True)
class AccountStatus:
    kind: AccountStatusKind
    cookie: str
    uid: int | None = None
    uname: str = ""


def check_cookie_account(cookie: str) -> tuple[int | None, str]:
    """Check one Cookie without exposing it to logs or exception messages."""

    async def check() -> tuple[int | None, str]:
        client = BilibiliClient(cookie)
        try:
            return await client.get_self_info()
        finally:
            await client.close()

    return asyncio.run(check())


class AccountStatusController(QObject):
    """Debounced, generation-safe account checks for the GUI."""

    _result_ready = Signal(int, str, object)

    def __init__(
        self,
        parent: QObject,
        *,
        get_cookie: Callable[[], str],
        on_status: Callable[[AccountStatus], None],
        checker: AccountChecker | None = None,
        debounce_ms: int = 400,
        recheck_ms: int = 60_000,
    ) -> None:
        super().__init__(parent)
        self._get_cookie = get_cookie
        self._on_status = on_status
        self._checker = checker
        self._generation = 0
        self._alive = True

        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(debounce_ms)
        self._debounce_timer.timeout.connect(self._start_pending_check)

        self._recheck_timer = QTimer(self)
        self._recheck_timer.setInterval(recheck_ms)
        self._recheck_timer.timeout.connect(self.recheck)

        self._result_ready.connect(self._apply_result, Qt.QueuedConnection)

    @property
    def generation(self) -> int:
        return self._generation

    def cookie_changed(self, _text: str = "") -> None:
        if not self._alive:
            return
        self._generation += 1
        self._debounce_timer.stop()
        cookie = self._get_cookie().strip()
        if not cookie:
            self._recheck_timer.stop()
            self._on_status(AccountStatus("empty", ""))
            return
        self._on_status(AccountStatus("checking", cookie))
        self._debounce_timer.start()
        self._recheck_timer.start()

    def recheck(self) -> None:
        if not self._alive or not self._get_cookie().strip():
            return
        self._generation += 1
        self._start_check(self._generation, self._get_cookie().strip())

    def mark_current_invalid(self) -> None:
        if not self._alive:
            return
        self._generation += 1
        self._debounce_timer.stop()
        self._on_status(AccountStatus("invalid", self._get_cookie().strip()))

    def _start_pending_check(self) -> None:
        cookie = self._get_cookie().strip()
        if not cookie:
            return
        self._start_check(self._generation, cookie)

    def _start_check(self, generation: int, cookie: str) -> None:
        def run() -> None:
            try:
                checker = self._checker or check_cookie_account
                uid, uname = checker(cookie)
                status = AccountStatus(
                    "valid" if uid is not None else "invalid",
                    cookie,
                    uid,
                    uname,
                )
            except Exception:
                status = AccountStatus("error", cookie)
            if self._alive:
                self._safe_emit_result(generation, cookie, status)

        threading.Thread(
            target=run,
            daemon=True,
            name="gui-account-check",
        ).start()

    def _safe_emit_result(
        self,
        generation: int,
        cookie: str,
        status: AccountStatus,
    ) -> None:
        try:
            self._result_ready.emit(generation, cookie, status)
        except RuntimeError:
            # The underlying QObject may be deleted between the worker's
            # alive check and signal attribute access during window teardown.
            pass

    def _apply_result(
        self,
        generation: int,
        cookie: str,
        status: AccountStatus,
    ) -> None:
        if (
            not self._alive
            or generation != self._generation
            or cookie != self._get_cookie().strip()
        ):
            return
        self._on_status(status)

    def close(self) -> None:
        if not self._alive:
            return
        self._alive = False
        self._generation += 1
        self._debounce_timer.stop()
        self._recheck_timer.stop()
