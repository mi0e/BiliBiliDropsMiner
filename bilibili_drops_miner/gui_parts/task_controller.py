from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable

from bilibili_drops_miner.client import BilibiliClient, LiveWatchTime
from bilibili_drops_miner.gui_parts.task_presenter import (
    format_live_watch_time_progress,
    format_reward_claim_results,
    format_task_progress,
)


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
    ) -> None:
        self._get_cookie = get_cookie
        self._get_room_ids = get_room_ids
        self._get_task_ids = get_task_ids
        self._show_warning = show_warning
        self._set_task_progress_text = set_task_progress_text
        self._set_live_watch_time_text = set_live_watch_time_text
        self._complete_task_refresh = complete_task_refresh
        self._post_ui_task = post_ui_task
        self._task_refresh_lock = threading.Lock()
        self._task_refresh_inflight = False
        self._task_refresh_queued = False
        self._reward_claim_lock = threading.Lock()
        self._reward_claim_inflight = False
        self._watch_time_lock = threading.Lock()
        self._watch_time_inflight = False
        self._watch_time_generation = 0
        self._watch_time_baselines: dict[int, int] = {}
        self._watch_time_ruids: dict[int, int] = {}

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
            try:

                async def _query():
                    client = BilibiliClient(cookie)
                    try:
                        return await client.get_task_progress(task_ids)
                    finally:
                        await client.close()

                progresses = asyncio.run(_query())
                result_text = format_task_progress(progresses)
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

        threading.Thread(target=_do, daemon=True, name="gui-task-refresh").start()

    def claim_rewards(self) -> None:
        cookie = self._get_cookie().strip()
        if not cookie:
            self._show_warning("提示", "请先填写 Cookie")
            return
        task_ids = self._get_task_ids()
        if not task_ids:
            self._show_warning("提示", "请先填写或自动获取任务 ID")
            return

        with self._reward_claim_lock:
            if self._reward_claim_inflight:
                self._set_task_progress_text("已有领奖任务进行中，请稍候...")
                return
            self._reward_claim_inflight = True

        self._set_task_progress_text("正在领取全部可领取奖励...")

        def _do() -> None:
            result_text = ""
            try:

                async def _claim():
                    client = BilibiliClient(cookie)
                    try:
                        return await client.receive_all_mission_rewards(task_ids)
                    finally:
                        await client.close()

                results = asyncio.run(_claim())
                result_text = format_reward_claim_results(results)
            except Exception as exc:
                logging.getLogger(__name__).warning("领取奖励失败: %s", exc)
                result_text = f"领取奖励失败: {exc}"
            finally:
                with self._reward_claim_lock:
                    self._reward_claim_inflight = False
                if result_text:
                    logging.getLogger(__name__).info("领取奖励结果:\n%s", result_text)
                self._post_ui_task(self._set_task_progress_text, result_text)

        threading.Thread(target=_do, daemon=True, name="gui-reward-claim").start()
