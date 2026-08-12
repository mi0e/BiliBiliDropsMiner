from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from collections.abc import Callable

from bilibili_drops_miner.client import BilibiliClient, LiveWatchTime
from bilibili_drops_miner.gui_parts.task_presenter import (
    format_live_watch_time_progress,
    format_reward_claim_results,
    format_task_progress,
)


AutoClaimContext = tuple[bytes, tuple[str, ...]]


def classify_auto_claim_task_ids(
    progresses: list,
) -> tuple[set[str], set[str], set[str]]:
    """Return completed, claimed and incomplete concrete reward task IDs."""
    completed: set[str] = set()
    claimed: set[str] = set()
    incomplete: set[str] = set()

    for task in progresses:
        checkpoints = [
            (str(getattr(point, "sid", "") or "").strip(), point)
            for point in (getattr(task, "check_points", None) or [])
        ]
        concrete_items = [
            (task_id, point) for task_id, point in checkpoints if task_id
        ]
        if not concrete_items:
            task_id = str(getattr(task, "task_id", "") or "").strip()
            concrete_items = [(task_id, task)] if task_id else []

        for task_id, item in concrete_items:
            if getattr(item, "status", None) == 6:
                claimed.add(task_id)
            elif bool(getattr(item, "is_completed", False)):
                completed.add(task_id)
            else:
                incomplete.add(task_id)

    return completed, claimed, incomplete


class TaskController:
    def __init__(
        self,
        *,
        get_cookie: Callable[[], str],
        get_room_ids: Callable[[], list[int]],
        get_task_ids: Callable[[], list[str]],
        show_warning: Callable[[str, str], None],
        set_task_progress_text: Callable[[str], None],
        set_live_watch_time_text: Callable[[str], None],
        complete_task_refresh: Callable[[str, bool], None],
        post_ui_task: Callable[..., None],
        get_auto_claim_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self._get_cookie = get_cookie
        self._get_room_ids = get_room_ids
        self._get_task_ids = get_task_ids
        self._show_warning = show_warning
        self._set_task_progress_text = set_task_progress_text
        self._set_live_watch_time_text = set_live_watch_time_text
        self._complete_task_refresh = complete_task_refresh
        self._post_ui_task = post_ui_task
        self._get_auto_claim_enabled = get_auto_claim_enabled or (lambda: False)
        self._task_refresh_lock = threading.Lock()
        self._task_refresh_inflight = False
        self._task_refresh_queued = False
        self._reward_claim_lock = threading.Lock()
        self._reward_claim_inflight = False
        self._auto_claim_context: AutoClaimContext | None = None
        self._auto_claim_handled_ids: set[str] = set()
        self._auto_claim_pending_ids: set[str] = set()
        self._watch_time_lock = threading.Lock()
        self._watch_time_inflight = False
        self._watch_time_generation = 0
        self._watch_time_baselines: dict[int, int] = {}
        self._watch_time_ruids: dict[int, int] = {}

    @staticmethod
    def _build_auto_claim_context(
        cookie: str,
        task_ids: list[str],
    ) -> AutoClaimContext:
        cookie_fingerprint = hashlib.sha256(cookie.encode("utf-8")).digest()
        normalized_ids = tuple(
            task_id.strip() for task_id in task_ids if task_id.strip()
        )
        return cookie_fingerprint, normalized_ids

    def _sync_auto_claim_context_locked(self, context: AutoClaimContext) -> None:
        if self._auto_claim_context == context:
            return
        self._auto_claim_context = context
        self._auto_claim_handled_ids.clear()
        self._auto_claim_pending_ids.clear()

    def _release_auto_claim_pending(
        self,
        context: AutoClaimContext | None,
        task_ids: set[str],
    ) -> None:
        if context is None or not task_ids:
            return
        with self._reward_claim_lock:
            if self._auto_claim_context == context:
                self._auto_claim_pending_ids.difference_update(task_ids)

    def reset_live_watch_time(self) -> None:
        with self._watch_time_lock:
            self._watch_time_generation += 1
            self._watch_time_inflight = False
            self._watch_time_baselines.clear()
            self._watch_time_ruids.clear()

    def stop_live_watch_time(self) -> None:
        with self._watch_time_lock:
            self._watch_time_generation += 1
            self._watch_time_inflight = False

    def refresh_live_watch_time(self) -> None:
        cookie = self._get_cookie().strip()
        if not cookie:
            self._set_live_watch_time_text("本次预估观看时长: 等待 Cookie")
            return

        room_ids = self._get_room_ids()
        if not room_ids:
            self._set_live_watch_time_text("本次预估观看时长: 未设置房间")
            return

        with self._watch_time_lock:
            if self._watch_time_inflight:
                return
            self._watch_time_inflight = True
            generation = self._watch_time_generation
            known_ruids = dict(self._watch_time_ruids)

        def _do() -> None:
            result_text = ""
            try:

                async def _query() -> tuple[list[LiveWatchTime], list[str]]:
                    client = BilibiliClient(cookie)
                    try:
                        watch_times: list[LiveWatchTime] = []
                        errors: list[str] = []
                        for room_id in room_ids:
                            try:
                                watch_time = await client.get_live_watch_time(
                                    room_id,
                                    ruid=known_ruids.get(room_id),
                                )
                                watch_times.append(watch_time)
                            except Exception as exc:
                                errors.append(f"房间 {room_id}: {exc}")
                        return watch_times, errors
                    finally:
                        await client.close()

                watch_times, errors = asyncio.run(_query())
                if not watch_times and errors:
                    raise ValueError("; ".join(errors))

                with self._watch_time_lock:
                    if generation != self._watch_time_generation:
                        return
                    for item in watch_times:
                        self._watch_time_ruids[item.room_id] = item.ruid
                        self._watch_time_baselines.setdefault(
                            item.room_id,
                            item.watch_time,
                        )
                    baselines = dict(self._watch_time_baselines)

                result_text = format_live_watch_time_progress(watch_times, baselines)
                if errors:
                    logging.getLogger(__name__).warning(
                        "刷新实时观看时长部分失败: %s",
                        "; ".join(errors),
                    )
                    result_text += f"（部分失败 {len(errors)} 个房间）"
            except Exception as exc:
                logging.getLogger(__name__).warning("刷新实时观看时长失败: %s", exc)
                result_text = f"本次预估观看时长: 刷新失败: {exc}"
            finally:
                should_update = False
                with self._watch_time_lock:
                    if generation == self._watch_time_generation:
                        self._watch_time_inflight = False
                        should_update = True
                if should_update and result_text:
                    self._post_ui_task(self._set_live_watch_time_text, result_text)

        threading.Thread(
            target=_do,
            daemon=True,
            name="gui-live-watch-time-refresh",
        ).start()

    def refresh(self, *, manual: bool = True) -> None:
        cookie = self._get_cookie().strip()
        if not cookie:
            if manual:
                self._show_warning("提示", "请先填写 Cookie")
            return
        task_ids = self._get_task_ids()
        if not task_ids:
            self._set_task_progress_text("无任务数据（未填写任务 ID）")
            return

        auto_claim_enabled = bool(self._get_auto_claim_enabled())
        auto_claim_context = self._build_auto_claim_context(cookie, task_ids)

        with self._task_refresh_lock:
            if self._task_refresh_inflight:
                self._task_refresh_queued = True
                if manual:
                    self._set_task_progress_text("已有刷新进行中，已排队下一次刷新...")
                return
            self._task_refresh_inflight = True

        self._set_task_progress_text("正在刷新任务进度...")

        def _do() -> None:
            result_text = ""
            auto_claim_task_ids: set[str] = set()
            try:

                async def _query():
                    client = BilibiliClient(cookie)
                    try:
                        return await client.get_task_progress(task_ids)
                    finally:
                        await client.close()

                progresses = asyncio.run(_query())
                result_text = format_task_progress(progresses)
                completed_ids, claimed_ids, incomplete_ids = (
                    classify_auto_claim_task_ids(progresses)
                )
                with self._reward_claim_lock:
                    self._sync_auto_claim_context_locked(auto_claim_context)
                    self._auto_claim_handled_ids.difference_update(incomplete_ids)
                    self._auto_claim_pending_ids.difference_update(
                        claimed_ids | incomplete_ids
                    )
                    if auto_claim_enabled:
                        auto_claim_task_ids = completed_ids.difference(
                            self._auto_claim_handled_ids,
                            self._auto_claim_pending_ids,
                        )
                        self._auto_claim_pending_ids.update(auto_claim_task_ids)
            except Exception as exc:
                logging.getLogger(__name__).warning("刷新任务失败: %s", exc)
                result_text = f"刷新任务失败: {exc}"
            finally:
                rerun = False
                with self._task_refresh_lock:
                    self._task_refresh_inflight = False
                    if self._task_refresh_queued:
                        rerun = True
                        self._task_refresh_queued = False
                self._post_ui_task(self._complete_task_refresh, result_text, rerun)
                if auto_claim_task_ids:
                    logging.getLogger(__name__).info(
                        "检测到 %s 个已完成任务，触发自动领取",
                        len(auto_claim_task_ids),
                    )
                    self._post_ui_task(
                        self.claim_rewards,
                        automatic=True,
                        trigger_task_ids=frozenset(auto_claim_task_ids),
                        expected_context=auto_claim_context,
                    )

        threading.Thread(target=_do, daemon=True, name="gui-task-refresh").start()

    def claim_rewards(
        self,
        *,
        automatic: bool = False,
        trigger_task_ids: frozenset[str] | None = None,
        expected_context: AutoClaimContext | None = None,
    ) -> None:
        auto_task_ids = set(trigger_task_ids or ())
        cookie = self._get_cookie().strip()
        if not cookie:
            self._release_auto_claim_pending(expected_context, auto_task_ids)
            if not automatic:
                self._show_warning("提示", "请先填写 Cookie")
            return
        task_ids = self._get_task_ids()
        if not task_ids:
            self._release_auto_claim_pending(expected_context, auto_task_ids)
            if not automatic:
                self._show_warning("提示", "请先填写或自动获取任务 ID")
            return

        claim_context = self._build_auto_claim_context(cookie, task_ids)
        if automatic and (
            not auto_task_ids
            or not self._get_auto_claim_enabled()
            or expected_context != claim_context
        ):
            self._release_auto_claim_pending(expected_context, auto_task_ids)
            return

        with self._reward_claim_lock:
            self._sync_auto_claim_context_locked(claim_context)
            if self._reward_claim_inflight:
                if automatic:
                    self._auto_claim_pending_ids.difference_update(auto_task_ids)
                else:
                    self._set_task_progress_text("已有领奖任务进行中，请稍候...")
                return
            self._reward_claim_inflight = True

        if automatic:
            self._set_task_progress_text("检测到任务完成，正在自动领取奖励...")
        else:
            self._set_task_progress_text("正在领取全部可领取奖励...")

        def _do() -> None:
            result_text = ""
            successful_ids: set[str] = set()
            try:

                async def _claim():
                    client = BilibiliClient(cookie)
                    try:
                        return await client.receive_all_mission_rewards(task_ids)
                    finally:
                        await client.close()

                results = asyncio.run(_claim())
                result_text = format_reward_claim_results(results)
                if automatic:
                    result_text = f"自动领取\n{result_text}"
                successful_ids = {
                    str(item.task_id).strip()
                    for item in results
                    if item.success and str(item.task_id).strip()
                }
            except Exception as exc:
                logging.getLogger(__name__).warning("领取奖励失败: %s", exc)
                prefix = "自动领取失败" if automatic else "领取奖励失败"
                result_text = f"{prefix}: {exc}"
            finally:
                with self._reward_claim_lock:
                    self._reward_claim_inflight = False
                    if self._auto_claim_context == claim_context:
                        self._auto_claim_pending_ids.difference_update(auto_task_ids)
                        self._auto_claim_handled_ids.update(successful_ids)
                if successful_ids:
                    logging.getLogger(__name__).info(
                        "领奖成功状态已标记: %s 个任务",
                        len(successful_ids),
                    )
                if result_text:
                    logging.getLogger(__name__).info("领取奖励结果:\n%s", result_text)
                self._post_ui_task(self._set_task_progress_text, result_text)

        threading.Thread(target=_do, daemon=True, name="gui-reward-claim").start()
