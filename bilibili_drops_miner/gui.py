from __future__ import annotations

import asyncio
import json
import logging
import queue
import re
import threading
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from bilibili_drops_miner.client import BilibiliClient
from bilibili_drops_miner.config import MinerConfig
from bilibili_drops_miner.logging_utils import setup_logging
from bilibili_drops_miner.miner import BilibiliWatchTimeMiner
from bilibili_drops_miner.utils import parse_room_ids, parse_task_ids


class QueueLogHandler(logging.Handler):
    def __init__(self, q: "queue.Queue[str]") -> None:
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put(self.format(record))
        except Exception:
            self.handleError(record)


class MinerGUI:
    def __init__(self, root: ctk.CTk) -> None:
        self.root = root
        self.root.title("Bilibili \u76f4\u64ad\u6389\u5b9d\u52a9\u624b")
        self.root.geometry("980x630")
        self.root.minsize(800, 500)
        self._size_expanded = "980x920"
        self._size_collapsed = "980x630"

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.miner: BilibiliWatchTimeMiner | None = None

        self.cookie_var = ctk.StringVar()
        self.rooms_var = ctk.StringVar(value="6")
        self.threads_var = ctk.StringVar(value="1")
        self.heartbeat_var = ctk.StringVar(value="30")
        self.reconnect_var = ctk.StringVar(value="8")
        self.task_ids_var = ctk.StringVar()
        self.task_interval_var = ctk.StringVar(value="30")
        self.notify_urls_var = ctk.StringVar()
        self.verbose_var = ctk.BooleanVar(value=False)
        self.disable_web_heartbeat_var = ctk.BooleanVar(value=False)
        self.disable_task_notify_var = ctk.BooleanVar(value=False)

        self._last_verbose: bool | None = None
        self._task_progress_result: str = ""
        self._task_progress_pending: bool = False

        self._build_layout()
        self._install_logging()
        self._schedule_log_flush()

    def _build_layout(self) -> None:
        # --- Config section ---
        config_frame = ctk.CTkFrame(self.root)
        config_frame.pack(fill="x", padx=16, pady=(16, 8))

        title = ctk.CTkLabel(
            config_frame,
            text="Bilibili \u76f4\u64ad\u6389\u5b9d\u52a9\u624b",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        title.pack(anchor="w", padx=16, pady=(12, 8))

        # Main input fields
        fields_frame = ctk.CTkFrame(config_frame, fg_color="transparent")
        fields_frame.pack(fill="x", padx=16, pady=(0, 4))

        self._add_entry(fields_frame, 0, "Cookie", self.cookie_var, placeholder="\u5fc5\u586b: SESSDATA=xxx; bili_jct=xxx; DedeUserID=xxx")

        self._add_entry(fields_frame, 1, "\u623f\u95f4\u53f7", self.rooms_var, placeholder="\u5fc5\u586b: \u76f4\u64ad\u95f4\u53f7\uff0c\u591a\u4e2a\u7528\u9017\u53f7\u5206\u9694")
        self._add_entry(fields_frame, 2, "\u4efb\u52a1 ID", self.task_ids_var, placeholder="\u53ef\u7559\u7a7a: F12 \u4ece totalv2 \u8bf7\u6c42\u4e2d\u63d0\u53d6 task_ids")

        self._add_entry(fields_frame, 3, "\u901a\u77e5 URL", self.notify_urls_var, placeholder="\u53ef\u7559\u7a7a: Apprise URL\uff0c\u5982 gotify://host/token")

        fields_frame.columnconfigure(1, weight=1)

        # Small numeric fields
        num_frame = ctk.CTkFrame(config_frame, fg_color="transparent")
        num_frame.pack(fill="x", padx=16, pady=(4, 4))

        self._add_small_entry(num_frame, 0, "\u7ebf\u7a0b\u6570", self.threads_var)
        self._add_small_entry(num_frame, 1, "WS \u5fc3\u8df3(s)", self.heartbeat_var)
        self._add_small_entry(
            num_frame, 2, "\u91cd\u8fde\u5ef6\u8fdf(s)", self.reconnect_var
        )
        self._add_small_entry(
            num_frame,
            3,
            "\u4efb\u52a1\u67e5\u8be2\u95f4\u9694(s)",
            self.task_interval_var,
        )

        # Toggles
        toggle_frame = ctk.CTkFrame(config_frame, fg_color="transparent")
        toggle_frame.pack(fill="x", padx=16, pady=(4, 8))

        ctk.CTkSwitch(
            toggle_frame,
            text="\u8be6\u7ec6\u65e5\u5fd7",
            variable=self.verbose_var,
            width=40,
        ).pack(side="left", padx=(0, 16))
        ctk.CTkSwitch(
            toggle_frame,
            text="\u7981\u7528 x25Kn \u5fc3\u8df3",
            variable=self.disable_web_heartbeat_var,
            width=40,
        ).pack(side="left", padx=(0, 16))
        ctk.CTkSwitch(
            toggle_frame,
            text="\u7981\u7528\u4efb\u52a1\u5b8c\u6210\u901a\u77e5",
            variable=self.disable_task_notify_var,
            width=40,
        ).pack(side="left", padx=(0, 16))

        # Buttons
        btn_frame = ctk.CTkFrame(config_frame, fg_color="transparent")
        btn_frame.pack(fill="x", padx=16, pady=(4, 12))

        ctk.CTkButton(
            btn_frame,
            text="\u542f\u52a8",
            width=100,
            command=self.start,
            fg_color="#2ecc71",
            hover_color="#27ae60",
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_frame,
            text="\u505c\u6b62",
            width=100,
            command=self.stop,
            fg_color="#e74c3c",
            hover_color="#c0392b",
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_frame,
            text="\u81ea\u52a8\u83b7\u53d6\u4efb\u52a1ID",
            width=120,
            command=self.auto_fetch_task_ids,
            fg_color="#3498db",
            hover_color="#2980b9",
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_frame,
            text="\u52a0\u8f7d\u914d\u7f6e",
            width=100,
            command=self.load_config,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_frame,
            text="\u4fdd\u5b58\u914d\u7f6e",
            width=100,
            command=self.save_config,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_frame,
            text="\u6e05\u7a7a\u65e5\u5fd7",
            width=100,
            command=self.clear_logs,
            fg_color="#95a5a6",
            hover_color="#7f8c8d",
        ).pack(side="left", padx=(0, 8))

        # --- Task Progress section ---
        task_frame = ctk.CTkFrame(self.root)
        task_frame.pack(fill="x", padx=16, pady=(0, 8))

        task_header = ctk.CTkFrame(task_frame, fg_color="transparent")
        task_header.pack(fill="x", padx=12, pady=(8, 4))

        ctk.CTkLabel(
            task_header,
            text="\u4efb\u52a1\u8fdb\u5ea6",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(side="left")
        ctk.CTkButton(
            task_header,
            text="\u624b\u52a8\u5237\u65b0",
            width=80,
            command=self.refresh_tasks,
        ).pack(side="right")

        self.task_text = ctk.CTkTextbox(
            task_frame,
            font=ctk.CTkFont(family="Consolas", size=12),
            wrap="none",
            height=180,
        )
        self.task_text.pack(fill="x", padx=8, pady=(0, 8))
        self.task_text.insert("1.0", "\u70b9\u51fb\u201c\u624b\u52a8\u5237\u65b0\u201d\u67e5\u770b\u4efb\u52a1\u8fdb\u5ea6")

        # --- Log section (collapsible, default collapsed) ---
        self._log_frame = ctk.CTkFrame(self.root)
        self._log_frame.pack(fill="x", padx=16, pady=(0, 16))

        log_header = ctk.CTkFrame(self._log_frame, fg_color="transparent")
        log_header.pack(fill="x", padx=12, pady=(8, 4))

        self._log_toggle_btn = ctk.CTkButton(
            log_header,
            text="\u25b6 \u8fd0\u884c\u65e5\u5fd7",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="transparent",
            hover_color=("gray75", "gray30"),
            text_color=("gray10", "gray90"),
            anchor="w",
            command=self._toggle_log,
        )
        self._log_toggle_btn.pack(side="left")

        self.log_text = ctk.CTkTextbox(
            self._log_frame, font=ctk.CTkFont(family="Consolas", size=12), wrap="word"
        )
        # Default collapsed - don't pack log_text
        self._log_expanded = False

    @staticmethod
    def _add_entry(
        parent: ctk.CTkFrame, row: int, label: str, text_var: ctk.StringVar,
        *, placeholder: str = "",
    ) -> None:
        ctk.CTkLabel(parent, text=label, width=100, anchor="w").grid(
            row=row, column=0, sticky="w", pady=3
        )
        ctk.CTkEntry(parent, textvariable=text_var, placeholder_text=placeholder).grid(
            row=row, column=1, sticky="ew", pady=3, padx=(4, 0)
        )

    @staticmethod
    def _add_small_entry(
        parent: ctk.CTkFrame, col: int, label: str, text_var: ctk.StringVar
    ) -> None:
        sub = ctk.CTkFrame(parent, fg_color="transparent")
        sub.pack(side="left", padx=(0, 16))
        ctk.CTkLabel(sub, text=label).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(sub, textvariable=text_var, width=70).pack(side="left")

    def _install_logging(self) -> None:
        queue_handler = QueueLogHandler(self.log_queue)
        setup_logging(
            verbose=self.verbose_var.get(),
            no_color=True,
            extra_handlers=[queue_handler],
        )

    def _schedule_log_flush(self) -> None:
        self._flush_log_queue()
        self.root.after(120, self._schedule_log_flush)

    def _flush_log_queue(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
        if self._task_progress_pending:
            self._task_progress_pending = False
            self.task_text.delete("1.0", "end")
            self.task_text.insert("1.0", self._task_progress_result)

    def _build_config(self) -> MinerConfig:
        return MinerConfig(
            cookie=self.cookie_var.get().strip(),
            room_ids=parse_room_ids(self.rooms_var.get().strip()),
            thread_count=int(self.threads_var.get().strip() or "1"),
            heartbeat_interval_seconds=int(self.heartbeat_var.get().strip() or "30"),
            reconnect_delay_seconds=int(self.reconnect_var.get().strip() or "8"),
            enable_web_heartbeat=not self.disable_web_heartbeat_var.get(),
            debug_events=False,
            task_ids=parse_task_ids(self.task_ids_var.get().strip()),
            task_query_interval_seconds=int(
                self.task_interval_var.get().strip() or "30"
            ),
            notify_urls=parse_task_ids(self.notify_urls_var.get().strip()),
            notify_on_task_complete=not self.disable_task_notify_var.get(),
        )

    def start(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo(
                "\u8fd0\u884c\u4e2d",
                "\u52a9\u624b\u5df2\u5728\u8fd0\u884c\u4e2d\u3002",
            )
            return
        try:
            self._install_logging()
            config = self._build_config()
            config.validate()
            self.miner = BilibiliWatchTimeMiner(config)
        except Exception as exc:
            messagebox.showerror("\u914d\u7f6e\u9519\u8bef", str(exc))
            return

        def runner() -> None:
            try:
                if self.miner is not None:
                    self.miner.run()
            except Exception:
                logging.getLogger(__name__).exception("GUI worker crashed")

        self.worker_thread = threading.Thread(
            target=runner, name="gui-main-worker", daemon=True
        )
        self.worker_thread.start()
        logging.getLogger(__name__).info("\u6389\u5b9d\u52a9\u624b\u5df2\u542f\u52a8")
        self._schedule_config_sync()
        self._schedule_task_refresh()

    def _toggle_log(self) -> None:
        if self._log_expanded:
            self.log_text.pack_forget()
            self._log_frame.pack_configure(fill="x", expand=False)
            self._log_toggle_btn.configure(text="\u25b6 \u8fd0\u884c\u65e5\u5fd7")
            self.root.geometry(self._size_collapsed)
        else:
            self._log_frame.pack_configure(fill="both", expand=True)
            self.log_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))
            self._log_toggle_btn.configure(text="\u25bc \u8fd0\u884c\u65e5\u5fd7")
            self.root.geometry(self._size_expanded)
        self._log_expanded = not self._log_expanded

    def stop(self) -> None:
        if self.miner is not None:
            self.miner.stop()
            logging.getLogger(__name__).info("\u6b63\u5728\u505c\u6b62...")

    def clear_logs(self) -> None:
        self.log_text.delete("1.0", "end")

    def refresh_tasks(self) -> None:
        cookie = self.cookie_var.get().strip()
        if not cookie:
            messagebox.showwarning("\u63d0\u793a", "\u8bf7\u5148\u586b\u5199 Cookie")
            return
        task_ids = parse_task_ids(self.task_ids_var.get().strip())
        if not task_ids:
            messagebox.showwarning("\u63d0\u793a", "\u8bf7\u5148\u586b\u5199\u4efb\u52a1 ID")
            return

        def _do() -> None:
            try:
                async def _query():
                    client = BilibiliClient(cookie)
                    try:
                        return await client.get_task_progress(task_ids)
                    finally:
                        await client.close()

                progresses = asyncio.run(_query())
                self._task_progress_result = self._format_task_progress(progresses)
                self._task_progress_pending = True
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "\u5237\u65b0\u4efb\u52a1\u5931\u8d25: %s", exc
                )

        threading.Thread(target=_do, daemon=True).start()

    def _browser_sniff(self, url_keyword: str, hint: str, on_match) -> None:
        """打开浏览器监听网络请求，匹配到 url_keyword 后调用 on_match(response)。"""

        def _do() -> None:
            try:
                from playwright.async_api import async_playwright
                import asyncio

                async def run():
                    done = []
                    async with async_playwright() as p:
                        browser = await p.chromium.launch(headless=False)
                        context = await browser.new_context()
                        page = await context.new_page()

                        async def handle_response(response):
                            if url_keyword in response.url:
                                try:
                                    await on_match(response)
                                    done.append(True)
                                except Exception:
                                    pass

                        page.on("response", handle_response)
                        await page.goto("https://www.bilibili.com/", wait_until="domcontentloaded")
                        logging.getLogger(__name__).info(hint)

                        for _ in range(120):
                            if done:
                                break
                            await asyncio.sleep(1)

                        await browser.close()
                    return bool(done)

                return asyncio.run(run())
            except ImportError:
                messagebox.showerror(
                    "\u4f9d\u8d56\u7f3a\u5931",
                    "Playwright \u672a\u5b89\u88c5\uff0c\u8bf7\u8fd0\u884c\uff1a\n\n"
                    "pip install playwright\n"
                    "playwright install chromium",
                )
                return False
            except Exception as exc:
                logging.getLogger(__name__).exception("\u81ea\u52a8\u83b7\u53d6\u5931\u8d25")
                messagebox.showerror("\u9519\u8bef", f"\u81ea\u52a8\u83b7\u53d6\u5931\u8d25: {exc}")
                return False

        threading.Thread(target=_do, daemon=True).start()

    def auto_fetch_task_ids(self) -> None:
        ok = messagebox.askokcancel(
            "\u81ea\u52a8\u83b7\u53d6\u4efb\u52a1ID",
            "\u70b9\u51fb\u786e\u5b9a\u540e\u4f1a\u6253\u5f00\u6d4f\u89c8\u5668\uff0c\u8bf7\u5728 2 \u5206\u949f\u5185\uff1a\n\n"
            "\u6253\u5f00\u6709\u5f53\u524d\u4efb\u52a1\u7684\u76f4\u64ad\u95f4\u5373\u53ef\u81ea\u52a8\u83b7\u53d6\uff0c\n"
            "\u6216\u624b\u52a8\u70b9\u51fb\u9875\u9762\u4e0a\u7684\u300c\u5237\u65b0\u4efb\u52a1\u300d\u6309\u94ae\u3002\n\n"
            "\u6355\u83b7\u6210\u529f\u540e\u6d4f\u89c8\u5668\u4f1a\u81ea\u52a8\u5173\u95ed\u3002",
        )
        if not ok:
            return

        async def on_match(response):
            data = await response.json()
            if data.get("code") != 0:
                raise ValueError("response code != 0")
            tasks = data.get("data", {}).get("list", [])
            task_ids = [t.get("task_id") for t in tasks if t.get("task_id")]
            if not task_ids:
                raise ValueError("empty task list")
            self.task_ids_var.set(",".join(task_ids))
            logging.getLogger(__name__).info("\u4efb\u52a1ID\u83b7\u53d6\u6210\u529f: %s", ",".join(task_ids))
            messagebox.showinfo("\u6210\u529f", f"\u5df2\u81ea\u52a8\u586b\u5165 {len(task_ids)} \u4e2a\u4efb\u52a1ID")

        self._browser_sniff(
            "/x/task/totalv2",
            "\u5df2\u6253\u5f00\u6d4f\u89c8\u5668\uff0c\u8bf7\u6253\u5f00\u6709\u5f53\u524d\u4efb\u52a1\u7684\u76f4\u64ad\u95f4\u6216\u70b9\u51fb\u5237\u65b0\u4efb\u52a1",
            on_match,
        )

    def _schedule_task_refresh(self) -> None:
        if self.worker_thread is None or not self.worker_thread.is_alive():
            return
        self.refresh_tasks()
        try:
            interval = int(self.task_interval_var.get().strip() or "30")
        except ValueError:
            interval = 30
        self.root.after(max(10, interval) * 1000, self._schedule_task_refresh)

    @staticmethod
    def _format_task_progress(progresses: list) -> str:
        if not progresses:
            return "\u65e0\u4efb\u52a1\u6570\u636e"

        _DURATION_RE = re.compile(r"^(.+?)\d+\u5206\u949f$")
        groups: dict[str, list] = {}
        for task in progresses:
            match = _DURATION_RE.match(task.task_name)
            prefix = match.group(1) if match else task.task_name
            groups.setdefault(prefix, []).append(task)
        for tasks in groups.values():
            tasks.sort(key=lambda t: float(t.limit_value))

        bar_width = 20
        lines: list[str] = []
        for prefix, tasks in groups.items():
            if len(tasks) > 1:
                cur = int(max(float(t.cur_value) for t in tasks))
                lines.append(f"{prefix} (\u5f53\u524d: {cur} \u5206\u949f)")
                for task in tasks:
                    target = int(float(task.limit_value))
                    pct = min(
                        100,
                        int(
                            float(task.cur_value)
                            / max(1, float(task.limit_value))
                            * 100
                        ),
                    )
                    filled = int(bar_width * pct / 100)
                    bar = "\u2588" * filled + "\u2591" * (bar_width - filled)
                    if task.is_completed:
                        status = " \u2714 \u5b8c\u6210"
                    else:
                        status = f" {pct:>3}%"
                    lines.append(f"  {bar} {target:>4}\u5206{status}")
            else:
                task = tasks[0]
                target = int(float(task.limit_value))
                cur = int(float(task.cur_value))
                pct = min(100, int(cur / max(1, target) * 100))
                filled = int(bar_width * pct / 100)
                bar = "\u2588" * filled + "\u2591" * (bar_width - filled)
                if task.is_completed:
                    status = " \u2714 \u5b8c\u6210"
                else:
                    status = f" {pct:>3}%"
                lines.append(f"{task.task_name} ({cur}/{target})")
                lines.append(f"  {bar}{status}")
        return "\n".join(lines)

    def _schedule_config_sync(self) -> None:
        self._sync_config_to_miner()
        if self.worker_thread is not None and self.worker_thread.is_alive():
            self.root.after(2000, self._schedule_config_sync)

    def _sync_config_to_miner(self) -> None:
        if self.miner is None:
            return
        config = self.miner.config

        try:
            val = int(self.heartbeat_var.get().strip() or "30")
            if val > 0:
                config.heartbeat_interval_seconds = val
        except ValueError:
            pass
        try:
            val = int(self.reconnect_var.get().strip() or "8")
            if val > 0:
                config.reconnect_delay_seconds = val
        except ValueError:
            pass
        try:
            val = int(self.task_interval_var.get().strip() or "30")
            if val > 0:
                config.task_query_interval_seconds = val
        except ValueError:
            pass

        config.enable_web_heartbeat = not self.disable_web_heartbeat_var.get()
        config.notify_on_task_complete = not self.disable_task_notify_var.get()

        verbose = self.verbose_var.get()
        config.debug_events = verbose
        if verbose != self._last_verbose:
            self._last_verbose = verbose
            self._install_logging()

        new_task_ids = parse_task_ids(self.task_ids_var.get().strip())
        if new_task_ids != config.task_ids:
            config.task_ids = new_task_ids

        new_cookie = self.cookie_var.get().strip()
        if new_cookie and new_cookie != config.cookie:
            self.miner.update_cookie(new_cookie)

        new_notify_urls = parse_task_ids(self.notify_urls_var.get().strip())
        if new_notify_urls != config.notify_urls:
            self.miner.update_notifier(new_notify_urls)

    def on_close(self) -> None:
        self.stop()
        self.root.after(150, self.root.destroy)

    def load_config(self) -> None:
        path = filedialog.askopenfilename(
            title="\u52a0\u8f7d\u914d\u7f6e\u6587\u4ef6",
            filetypes=[
                ("JSON \u6587\u4ef6", "*.json"),
                ("\u6240\u6709\u6587\u4ef6", "*.*"),
            ],
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.cookie_var.set(str(data.get("cookie", "")))
            self.rooms_var.set(",".join(str(x) for x in data.get("room_ids", [])))
            self.threads_var.set(str(data.get("thread_count", 1)))
            self.heartbeat_var.set(str(data.get("heartbeat_interval_seconds", 30)))
            self.reconnect_var.set(str(data.get("reconnect_delay_seconds", 8)))
            self.task_ids_var.set(",".join(str(x) for x in data.get("task_ids", [])))
            self.task_interval_var.set(str(data.get("task_query_interval_seconds", 30)))
            self.notify_urls_var.set(
                ",".join(str(x) for x in data.get("notify_urls", []))
            )
            self.disable_web_heartbeat_var.set(
                not bool(data.get("enable_web_heartbeat", True))
            )
            self.disable_task_notify_var.set(
                not bool(data.get("notify_on_task_complete", True))
            )
            self.verbose_var.set(bool(data.get("verbose", False)))
            logging.getLogger(__name__).info("\u914d\u7f6e\u5df2\u52a0\u8f7d: %s", path)
        except Exception as exc:
            messagebox.showerror("\u52a0\u8f7d\u5931\u8d25", str(exc))

    def save_config(self) -> None:
        path = filedialog.asksaveasfilename(
            title="\u4fdd\u5b58\u914d\u7f6e\u6587\u4ef6",
            defaultextension=".json",
            filetypes=[
                ("JSON \u6587\u4ef6", "*.json"),
                ("\u6240\u6709\u6587\u4ef6", "*.*"),
            ],
        )
        if not path:
            return
        try:
            config = self._build_config()
            data = {
                "cookie": config.cookie,
                "room_ids": config.room_ids,
                "thread_count": config.thread_count,
                "heartbeat_interval_seconds": config.heartbeat_interval_seconds,
                "reconnect_delay_seconds": config.reconnect_delay_seconds,
                "enable_web_heartbeat": config.enable_web_heartbeat,
                "task_ids": config.task_ids,
                "task_query_interval_seconds": config.task_query_interval_seconds,
                "notify_urls": config.notify_urls,
                "notify_on_task_complete": config.notify_on_task_complete,
                "verbose": self.verbose_var.get(),
            }
            Path(path).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logging.getLogger(__name__).info("\u914d\u7f6e\u5df2\u4fdd\u5b58: %s", path)
        except Exception as exc:
            messagebox.showerror("\u4fdd\u5b58\u5931\u8d25", str(exc))


def run_gui() -> int:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    app = MinerGUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0
