from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import threading
import time
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
        self.root.title("Bilibili 直播掉宝助手")
        self.root.geometry("980x630")
        self.root.minsize(800, 500)
        self._size_expanded = "980x920"
        self._size_collapsed = "980x630"

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.miner: BilibiliWatchTimeMiner | None = None
        self._stop_signal_set = False

        self.cookie_var = ctk.StringVar()
        self.rooms_var = ctk.StringVar(value="23612045")
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
        self._task_refresh_lock = threading.Lock()
        self._task_refresh_inflight: bool = False
        self._task_refresh_queued: bool = False
        self._task_refresh_trigger_pending: bool = False
        self._stopping_in_progress: bool = False
        self._stop_poll_started_at: float | None = None
        self._stop_timeout_warned: bool = False
        self._stop_force_sent: bool = False
        self._auto_force_stop_after_seconds: float = 2.0

        self._build_layout()
        self._install_logging()
        self._schedule_log_flush()

    def _build_layout(self) -> None:
        # --- Config section ---
        config_frame = ctk.CTkFrame(self.root)
        config_frame.pack(fill="x", padx=16, pady=(16, 8))

        title = ctk.CTkLabel(
            config_frame,
            text="Bilibili 直播掉宝助手",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        title.pack(anchor="w", padx=16, pady=(12, 8))

        # Main input fields
        fields_frame = ctk.CTkFrame(config_frame, fg_color="transparent")
        fields_frame.pack(fill="x", padx=16, pady=(0, 4))

        self._add_entry(
            fields_frame,
            0,
            "Cookie",
            self.cookie_var,
            placeholder="必填: SESSDATA=xxx; bili_jct=xxx; DedeUserID=xxx",
        )
        ctk.CTkButton(
            fields_frame,
            text="自动获取",
            width=80,
            command=self.auto_fetch_cookie,
            fg_color="#9b59b6",
            hover_color="#8e44ad",
        ).grid(row=0, column=2, padx=(4, 0), pady=3)

        self._add_entry(
            fields_frame,
            1,
            "房间号",
            self.rooms_var,
            placeholder="必填: 直播间号，多个用逗号分隔",
        )
        self._add_entry(
            fields_frame,
            2,
            "任务 ID",
            self.task_ids_var,
            placeholder="可留空: F12 从 totalv2 请求中提取 task_ids",
        )
        ctk.CTkButton(
            fields_frame,
            text="自动获取",
            width=80,
            command=self.auto_fetch_task_ids,
            fg_color="#3498db",
            hover_color="#2980b9",
        ).grid(row=2, column=2, padx=(4, 0), pady=3)

        self._add_entry(
            fields_frame,
            3,
            "通知 URL",
            self.notify_urls_var,
            placeholder="可留空: Apprise URL，如 gotify://host/token",
        )

        fields_frame.columnconfigure(1, weight=1)

        # Small numeric fields
        num_frame = ctk.CTkFrame(config_frame, fg_color="transparent")
        num_frame.pack(fill="x", padx=16, pady=(4, 4))

        self._add_small_entry(num_frame, 0, "线程数", self.threads_var)
        self._add_small_entry(num_frame, 1, "WS 心跳(s)", self.heartbeat_var)
        self._add_small_entry(num_frame, 2, "重连延迟(s)", self.reconnect_var)
        self._add_small_entry(
            num_frame,
            3,
            "任务查询间隔(s)",
            self.task_interval_var,
        )

        # Toggles
        toggle_frame = ctk.CTkFrame(config_frame, fg_color="transparent")
        toggle_frame.pack(fill="x", padx=16, pady=(4, 8))

        ctk.CTkSwitch(
            toggle_frame,
            text="详细日志",
            variable=self.verbose_var,
            width=40,
        ).pack(side="left", padx=(0, 16))
        ctk.CTkSwitch(
            toggle_frame,
            text="禁用 x25Kn 心跳",
            variable=self.disable_web_heartbeat_var,
            width=40,
        ).pack(side="left", padx=(0, 16))
        ctk.CTkSwitch(
            toggle_frame,
            text="禁用任务完成通知",
            variable=self.disable_task_notify_var,
            width=40,
        ).pack(side="left", padx=(0, 16))

        # Buttons
        btn_frame = ctk.CTkFrame(config_frame, fg_color="transparent")
        btn_frame.pack(fill="x", padx=16, pady=(4, 12))

        ctk.CTkButton(
            btn_frame,
            text="启动",
            width=100,
            command=self.start,
            fg_color="#2ecc71",
            hover_color="#27ae60",
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_frame,
            text="停止",
            width=100,
            command=self.stop,
            fg_color="#e74c3c",
            hover_color="#c0392b",
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_frame,
            text="加载配置",
            width=100,
            command=self.load_config,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_frame,
            text="保存配置",
            width=100,
            command=self.save_config,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_frame,
            text="清空日志",
            width=100,
            command=self.clear_logs,
            fg_color="#95a5a6",
            hover_color="#7f8c8d",
        ).pack(side="left", padx=(0, 8))

        # --- Running indicator (sliding block on canvas) ---
        self._progress_canvas = ctk.CTkCanvas(
            config_frame,
            height=4,
            highlightthickness=0,
            bg=config_frame.cget("fg_color")
            if isinstance(config_frame.cget("fg_color"), str)
            else config_frame.cget("fg_color")[1],
        )
        self._progress_running = False
        # hidden until running

        # --- Task Progress section ---
        task_frame = ctk.CTkFrame(self.root)
        task_frame.pack(fill="x", padx=16, pady=(0, 8))

        task_header = ctk.CTkFrame(task_frame, fg_color="transparent")
        task_header.pack(fill="x", padx=12, pady=(8, 4))

        ctk.CTkLabel(
            task_header,
            text="任务进度",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(side="left")
        ctk.CTkButton(
            task_header,
            text="手动刷新",
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
        self.task_text.insert("1.0", "点击“手动刷新”查看任务进度")

        # --- Log section (collapsible, default collapsed) ---
        self._log_frame = ctk.CTkFrame(self.root)
        self._log_frame.pack(fill="x", padx=16, pady=(0, 16))

        log_header = ctk.CTkFrame(self._log_frame, fg_color="transparent")
        log_header.pack(fill="x", padx=12, pady=(8, 4))

        self._log_toggle_btn = ctk.CTkButton(
            log_header,
            text="▶ 运行日志",
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
        parent: ctk.CTkFrame,
        row: int,
        label: str,
        text_var: ctk.StringVar,
        *,
        placeholder: str = "",
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
        if self._task_refresh_trigger_pending:
            self._task_refresh_trigger_pending = False
            self.refresh_tasks(manual=False)

    def _build_config(self) -> MinerConfig:
        return MinerConfig(
            cookie=self.cookie_var.get().strip(),
            room_ids=parse_room_ids(self.rooms_var.get().strip()),
            thread_count=int(self.threads_var.get().strip() or "1"),
            heartbeat_interval_seconds=int(self.heartbeat_var.get().strip() or "30"),
            reconnect_delay_seconds=int(self.reconnect_var.get().strip() or "8"),
            enable_web_heartbeat=not self.disable_web_heartbeat_var.get(),
            task_ids=parse_task_ids(self.task_ids_var.get().strip()),
            task_query_interval_seconds=int(
                self.task_interval_var.get().strip() or "30"
            ),
            notify_urls=parse_task_ids(self.notify_urls_var.get().strip()),
            notify_on_task_complete=not self.disable_task_notify_var.get(),
        )

    def start(self) -> None:
        self._stop_signal_set = False
        self._stopping_in_progress = False
        self._stop_poll_started_at = None
        self._stop_timeout_warned = False
        self._stop_force_sent = False
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo(
                "运行中",
                "助手已在运行中。",
            )
            return
        try:
            self._install_logging()
            config = self._build_config()
            config.validate()
            self.miner = BilibiliWatchTimeMiner(config)
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
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
        logging.getLogger(__name__).info("掉宝助手已启动")
        self._start_progress_animation()
        self._schedule_config_sync()
        self._schedule_task_refresh()

    def _toggle_log(self) -> None:
        if self._log_expanded:
            self.log_text.pack_forget()
            self._log_frame.pack_configure(fill="x", expand=False)
            self._log_toggle_btn.configure(text="▶ 运行日志")
            self.root.geometry(self._size_collapsed)
        else:
            self._log_frame.pack_configure(fill="both", expand=True)
            self.log_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))
            self._log_toggle_btn.configure(text="▼ 运行日志")
            self.root.geometry(self._size_expanded)
        self._log_expanded = not self._log_expanded

    def stop(self) -> None:
        self._stop_signal_set = True
        logger = logging.getLogger(__name__)
        self._stop_progress_animation()
        if self.worker_thread is None or not self.worker_thread.is_alive():
            self.worker_thread = None
            self.miner = None
            self._stopping_in_progress = False
            self._stop_poll_started_at = None
            self._stop_timeout_warned = False
            self._stop_force_sent = False
            return
        if self._stopping_in_progress:
            if self.miner is not None and not self._stop_force_sent:
                self._stop_force_sent = True
                self.miner.stop(force=True)
                logger.warning("已发送强制停止请求")
            else:
                logger.info("正在停止，请稍候...")
            return
        self._stopping_in_progress = True
        self._stop_poll_started_at = time.monotonic()
        self._stop_timeout_warned = False
        self._stop_force_sent = False
        if self.miner is not None:
            self.miner.stop(force=False)
        logger.info("正在停止...")
        self._poll_worker_shutdown()

    def _poll_worker_shutdown(self) -> None:
        logger = logging.getLogger(__name__)
        if self.worker_thread is None:
            self.miner = None
            self._stopping_in_progress = False
            self._stop_poll_started_at = None
            self._stop_timeout_warned = False
            self._stop_force_sent = False
            return
        if self.worker_thread.is_alive():
            if self._stop_poll_started_at is None:
                self._stop_poll_started_at = time.monotonic()
            elapsed = time.monotonic() - self._stop_poll_started_at
            if (
                elapsed >= self._auto_force_stop_after_seconds
                and not self._stop_force_sent
                and self.miner is not None
            ):
                self._stop_force_sent = True
                self.miner.stop(force=True)
                logger.warning(
                    "停止超过 %.1f 秒，已切换为强制停止",
                    self._auto_force_stop_after_seconds,
                )
            if elapsed >= 5 and not self._stop_timeout_warned:
                logger.warning("停止超过 5 秒，后台线程仍在退出中")
                self._stop_timeout_warned = True
            self.root.after(120, self._poll_worker_shutdown)
            return

        logger.info("停止成功")
        self.worker_thread = None
        self.miner = None
        self._stopping_in_progress = False
        self._stop_poll_started_at = None
        self._stop_timeout_warned = False
        self._stop_force_sent = False

    def _start_progress_animation(self) -> None:
        self._progress_running = True
        self._progress_pos = 0.0
        self._progress_dir = 1
        canvas = self._progress_canvas
        canvas.pack(fill="x", padx=16, pady=(0, 4))

        def _tick():
            if not self._progress_running:
                return
            self._progress_pos += 0.004 * self._progress_dir
            if self._progress_pos >= 1.0:
                self._progress_pos = 1.0
                self._progress_dir = -1
            elif self._progress_pos <= 0.0:
                self._progress_pos = 0.0
                self._progress_dir = 1

            w = canvas.winfo_width()
            if w < 2:
                self.root.after(20, _tick)
                return
            block_w = max(40, w // 6)
            x = self._progress_pos * (w - block_w)
            canvas.delete("all")
            # track
            canvas.create_rectangle(0, 1, w, 3, fill="#333333", outline="")
            # sliding block
            canvas.create_rectangle(
                x,
                0,
                x + block_w,
                4,
                fill="#3498db",
                outline="",
            )
            self.root.after(8, _tick)

        _tick()

    def _stop_progress_animation(self) -> None:
        self._progress_running = False
        self._progress_canvas.delete("all")
        self._progress_canvas.pack_forget()

    def clear_logs(self) -> None:
        self.log_text.delete("1.0", "end")

    def refresh_tasks(self, *, manual: bool = True) -> None:
        cookie = self.cookie_var.get().strip()
        if not cookie:
            if manual:
                messagebox.showwarning("提示", "请先填写 Cookie")
            return
        task_ids = parse_task_ids(self.task_ids_var.get().strip())
        if not task_ids:
            self._task_progress_pending = True
            self._task_progress_result = "无任务数据（未填写任务 ID）"
            return

        with self._task_refresh_lock:
            if self._task_refresh_inflight:
                self._task_refresh_queued = True
                if manual:
                    self._task_progress_result = "已有刷新进行中，已排队下一次刷新..."
                    self._task_progress_pending = True
                return
            self._task_refresh_inflight = True

        self._task_progress_result = "正在刷新任务进度..."
        self._task_progress_pending = True

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
                result_text = self._format_task_progress(progresses)
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

                self._task_progress_result = result_text
                self._task_progress_pending = True
                if rerun:
                    self._task_refresh_trigger_pending = True

        threading.Thread(target=_do, daemon=True, name="gui-task-refresh").start()

    def _find_browser(name: str) -> bool:
        if name == "edge":
            paths = [
                r"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
                r"C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
            ]
        else:
            paths = [
                r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
                r"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
                os.path.expandvars(
                    r"%LOCALAPPDATA%\\Google\\Chrome\Application\\chrome.exe"
                ),
            ]
        return any(os.path.exists(p) for p in paths)

    def _browser_sniff(
        self,
        url_keyword: str | None,
        hint: str,
        on_network_match=None,
        on_cookies=None,
    ) -> None:
        def _do() -> None:
            server = None
            ext_dir = None
            driver = None
            browser_type = None
            cdp_session = None
            try:
                import json
                import os
                import tempfile
                import time
                from http.server import BaseHTTPRequestHandler, HTTPServer

                from selenium import webdriver

                need_net = bool(url_keyword and on_network_match)
                need_cookie = on_cookies is not None

                net_captured: list = []
                cookie_captured: list = []

                # ---- 本地 HTTP 服务 ----
                class _Handler(BaseHTTPRequestHandler):
                    def do_POST(self):
                        length = int(self.headers.get("Content-Length", 0))
                        body = self.rfile.read(length)
                        try:
                            data = json.loads(body)
                            if data.get("type") == "__bili_cookies__":
                                cookie_captured.append(data["cookies"])
                            else:
                                net_captured.append(data)
                        except Exception:
                            pass
                        self.send_response(204)
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()

                    def do_OPTIONS(self):
                        self.send_response(204)
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Access-Control-Allow-Methods", "POST")
                        self.send_header("Access-Control-Allow-Headers", "Content-Type")
                        self.end_headers()

                    def log_message(self, *_a):
                        pass

                server = HTTPServer(("127.0.0.1", 0), _Handler)
                port = server.server_address[1]
                threading.Thread(target=server.serve_forever, daemon=True).start()

                # ---- 构建扩展 ----
                ext_dir = tempfile.mkdtemp(prefix="bili_sniff_")

                def _write_ext_edge() -> None:
                    manifest: dict = {
                        "manifest_version": 3,
                        "name": "BiliSniff",
                        "version": "1.0",
                        "host_permissions": ["http://127.0.0.1/*"],
                        "content_scripts": [],
                    }
                    files: dict[str, str] = {}

                    if need_net:
                        manifest["content_scripts"] += [
                            {
                                "matches": ["*://*.bilibili.com/*"],
                                "js": ["inject.js"],
                                "run_at": "document_start",
                                "world": "MAIN",
                            },
                            {
                                "matches": ["*://*.bilibili.com/*"],
                                "js": ["relay.js"],
                                "run_at": "document_start",
                            },
                        ]
                        files["inject.js"] = (
                            "(function(){\n"
                            "var origFetch=window.fetch;\n"
                            "window.fetch=async function(){\n"
                            "  var resp=await origFetch.apply(this,arguments);\n"
                            "  var url=(typeof arguments[0]==='string')?arguments[0]:arguments[0].url;\n"
                            "  if(url.indexOf('" + (url_keyword or "") + "')!==-1){\n"
                            "    try{var d=await resp.clone().json();\n"
                            "      window.postMessage({type:'__bili_sniff__',payload:{url:url,data:d}},'*');\n"
                            "    }catch(e){}\n"
                            "  }\n"
                            "  return resp;\n"
                            "};\n"
                            "var origOpen=XMLHttpRequest.prototype.open;\n"
                            "var origSend=XMLHttpRequest.prototype.send;\n"
                            "XMLHttpRequest.prototype.open=function(m,u){\n"
                            "  this.__url=u;return origOpen.apply(this,arguments);};\n"
                            "XMLHttpRequest.prototype.send=function(){\n"
                            "  var self=this;\n"
                            "  this.addEventListener('load',function(){\n"
                            "    if(self.__url&&self.__url.indexOf('"
                            + (url_keyword or "")
                            + "')!==-1){\n"
                            "      try{window.postMessage({type:'__bili_sniff__',\n"
                            "        payload:{url:self.__url,data:JSON.parse(self.responseText)}},'*');\n"
                            "      }catch(e){}\n"
                            "    }\n"
                            "  });\n"
                            "  return origSend.apply(this,arguments);\n"
                            "};\n"
                            "})();"
                        )
                        files["relay.js"] = (
                            "window.addEventListener('message',function(e){\n"
                            "  if(e.data&&e.data.type==='__bili_sniff__'){\n"
                            "    fetch('http://127.0.0.1:" + str(port) + "/',{\n"
                            "      method:'POST',\n"
                            "      headers:{'Content-Type':'application/json'},\n"
                            "      body:JSON.stringify(e.data.payload)\n"
                            "    }).catch(function(){});\n"
                            "  }\n"
                            "});"
                        )

                    if need_cookie:
                        manifest["permissions"] = ["cookies"]
                        manifest["host_permissions"].append("*://*.bilibili.com/*")
                        manifest["background"] = {"service_worker": "background.js"}
                        files["background.js"] = (
                            "function checkCookies(){\n"
                            "  chrome.cookies.getAll({domain:'.bilibili.com'},function(cookies){\n"
                            "    if(!cookies.some(function(c){return c.name==='SESSDATA';}))return;\n"
                            "    fetch('http://127.0.0.1:" + str(port) + "/',{\n"
                            "      method:'POST',\n"
                            "      headers:{'Content-Type':'application/json'},\n"
                            "      body:JSON.stringify({type:'__bili_cookies__',cookies:cookies})\n"
                            "    }).catch(function(){});\n"
                            "  });\n"
                            "}\n"
                            "checkCookies();\n"
                            "setInterval(checkCookies,3000);"
                        )

                    with open(
                        os.path.join(ext_dir, "manifest.json"), "w", encoding="utf-8"
                    ) as f:
                        json.dump(manifest, f)
                    for fname, content in files.items():
                        with open(
                            os.path.join(ext_dir, fname), "w", encoding="utf-8"
                        ) as f:
                            f.write(content)

                def _write_ext_chrome() -> None:
                    port = server.server_address[1]
                    relay_js = (
                        "window.addEventListener('message',function(e){\n"
                        "  if(e.data && e.data.type==='__bili_sniff__'){\n"
                        "    fetch('http://127.0.0.1:" + str(port) + "/',{\n"
                        "      method:'POST',\n"
                        "      headers:{'Content-Type':'application/json'},\n"
                        "      body:JSON.stringify(e.data.payload)\n"
                        "    }).catch(function(){});\n"
                        "  }\n"
                        "});"
                    )

                    background_js = (
                        "var injectedTabs = {};\n"
                        "var PORT = " + str(port) + ";\n"
                        "function injectTab(tabId) {\n"
                        "  if (injectedTabs[tabId]) return;\n"
                        "  injectedTabs[tabId] = true;\n"
                        "  chrome.scripting.executeScript({\n"
                        "    target: {tabId: tabId},\n"
                        "    world: 'ISOLATED',\n"
                        "    func: function() {\n"
                        "      window.addEventListener('message', function(e) {\n"
                        "        if (e.data && e.data.type === '__bili_sniff__') {\n"
                        "          fetch('http://127.0.0.1:" + str(port) + "/', {\n"
                        "            method: 'POST',\n"
                        "            headers: {'Content-Type': 'application/json'},\n"
                        "            body: JSON.stringify(e.data.payload)\n"
                        "          }).catch(function(){});\n"
                        "        }\n"
                        "      });\n"
                        "    }\n"
                        "  });\n"
                        "  chrome.scripting.executeScript({\n"
                        "    target: {tabId: tabId},\n"
                        "    world: 'MAIN',\n"
                        "    func: function() {\n"
                        "      if (window.__bili_sniff_injected__) return;\n"
                        "      window.__bili_sniff_injected__ = true;\n"
                        "      var kw = '" + (url_keyword or "") + "';\n"
                        "      // fetch 攔截（同你原本的邏輯）\n"
                        "      var origFetch = window.fetch;\n"
                        "      window.fetch = async function() {\n"
                        "        var resp = await origFetch.apply(this, arguments);\n"
                        "        var url = (typeof arguments[0] === 'string') ? arguments[0] : arguments[0].url;\n"
                        "        if (url.indexOf(kw) !== -1) {\n"
                        "          try {\n"
                        "            var d = await resp.clone().json();\n"
                        "            window.postMessage({type: '__bili_sniff__', payload: {url: url, data: d}}, '*');\n"
                        "          } catch(e) {}\n"
                        "        }\n"
                        "        return resp;\n"
                        "      };\n"
                        "      // XHR 攔截（保持你原本邏輯）\n"
                        "      var origOpen = XMLHttpRequest.prototype.open;\n"
                        "      var origSend = XMLHttpRequest.prototype.send;\n"
                        "      XMLHttpRequest.prototype.open = function(m, u) {\n"
                        "        this.__url = u; return origOpen.apply(this, arguments);\n"
                        "      };\n"
                        "      XMLHttpRequest.prototype.send = function() {\n"
                        "        var self = this;\n"
                        "        this.addEventListener('load', function() {\n"
                        "          if (self.__url && self.__url.indexOf(kw) !== -1) {\n"
                        "            try {\n"
                        "              window.postMessage({type: '__bili_sniff__', payload: {url: self.__url, data: JSON.parse(self.responseText)}}, '*');\n"
                        "            } catch(e) {}\n"
                        "          }\n"
                        "        });\n"
                        "        return origSend.apply(this, arguments);\n"
                        "      };\n"
                        "    }\n"
                        "  });\n"
                        "}\n"
                        "chrome.tabs.onUpdated.addListener(function(tabId, changeInfo, tab) {\n"
                        "  if (changeInfo.status === 'complete' && tab.url && tab.url.indexOf('bilibili.com') !== -1) {\n"
                        "    injectTab(tabId);\n"
                        "  }\n"
                        "});\n"
                        "chrome.tabs.query({url: '*://*.bilibili.com/*'}, function(tabs) {\n"
                        "  tabs.forEach(function(tab) { injectTab(tab.id); });\n"
                        "});\n"
                        "function sendCookies() {\n"
                        "  chrome.cookies.getAll({domain: '.bilibili.com'}, function(cookies) {\n"
                        "    if (cookies && cookies.length > 0) {\n"
                        "      fetch('http://127.0.0.1:' + PORT + '/', {\n"
                        "        method: 'POST',\n"
                        "        headers: {'Content-Type': 'application/json'},\n"
                        "        body: JSON.stringify({type: '__bili_cookies__', cookies: cookies})\n"
                        "      }).catch(function(){});\n"
                        "    }\n"
                        "  });\n"
                        "}\n"
                        "sendCookies();\n"
                        "setInterval(sendCookies, 3000);\n"
                        "chrome.cookies.onChanged.addListener(function(changeInfo) {\n"
                        "  if (changeInfo.cookie.domain.includes('bilibili.com')) {\n"
                        "    sendCookies();\n"
                        "  }\n"
                        "});\n"
                    )

                    manifest = {
                        "manifest_version": 3,
                        "name": "BiliSniff",
                        "version": "1.0",
                        "permissions": ["scripting", "tabs", "cookies"],
                        "host_permissions": [
                            "http://127.0.0.1/*",
                            "*://*.bilibili.com/*",
                        ],
                        "background": {"service_worker": "background.js"},
                        "content_scripts": [],
                    }

                    if need_net:
                        manifest["content_scripts"].append(
                            {
                                "matches": ["*://*.bilibili.com/*"],
                                "js": ["relay.js"],
                                "run_at": "document_start",
                            }
                        )

                    ext_path = os.path.join(ext_dir, "manifest.json")
                    with open(ext_path, "w", encoding="utf-8") as f:
                        json.dump(manifest, f, indent=2)

                    with open(
                        os.path.join(ext_dir, "background.js"), "w", encoding="utf-8"
                    ) as f:
                        f.write(background_js)

                    if need_net:
                        with open(
                            os.path.join(ext_dir, "relay.js"), "w", encoding="utf-8"
                        ) as f:
                            f.write(relay_js)

                last_exc = None
                for _browser in ("edge", "chrome"):
                    if not MinerGUI._find_browser(_browser):
                        logging.getLogger(__name__).info("未检测到 %s，跳过", _browser)
                        continue
                    try:
                        if _browser == "edge":
                            _write_ext_edge()
                            _opts = webdriver.EdgeOptions()
                            _opts.add_argument(f"--load-extension={ext_dir}")
                            driver = webdriver.Edge(options=_opts)
                            browser_type = "edge"
                        else:
                            _write_ext_chrome()
                            _opts = webdriver.ChromeOptions()
                            _opts.enable_bidi = True
                            _opts.enable_webextensions = True
                            _opts.add_argument("--remote-allow-origins=*")
                            driver = webdriver.Chrome(options=_opts)
                            browser_type = "chrome"
                            try:
                                ext_result = driver.webextension.install(path=ext_dir)
                            except Exception as e:
                                logging.getLogger(__name__).error(
                                    "安裝 extension 失败: %s", e
                                )

                        break
                    except Exception as _e:
                        last_exc = _e
                        driver = None
                        browser_type = None
                        logging.getLogger(__name__).warning(
                            "浏览器 %s 启动失败: %s", _browser, _e
                        )

                if driver is None:
                    raise RuntimeError(
                        f"未找到可用浏览器（Edge/Chrome），请确认已安装并配置好 WebDriver。\n最后错误: {last_exc}"
                    )

                driver.get("https://www.bilibili.com/")
                logging.getLogger(__name__).info(hint)

                # ---- 主等待循环 ----
                cookie_done = False
                net_done = False
                last_cookie_count = 0
                for i in range(120):
                    if need_cookie and not cookie_done and cookie_captured:
                        current_cookies = cookie_captured[-1]

                        has_sessdata = any(
                            c.get("name") == "SESSDATA" for c in current_cookies
                        )
                        has_dedeuid = any(
                            c.get("name") == "DedeUserID" for c in current_cookies
                        )

                        if has_sessdata and has_dedeuid:
                            filtered_cookies = [
                                c
                                for c in current_cookies
                                if c.get("name")
                                in [
                                    "SESSDATA",
                                    "bili_jct",
                                    "DedeUserID",
                                    "DedeUserID__ckMd5",
                                    "buvid3",
                                    "b_nut",
                                    "sid",
                                ]
                            ]
                            on_cookies(filtered_cookies)
                            cookie_done = True
                            logging.getLogger(__name__).info("已检测到登入 Cookie")
                        else:
                            if len(cookie_captured) > last_cookie_count:
                                last_cookie_count = len(cookie_captured)

                    if need_net and not net_done and net_captured:
                        on_network_match(net_captured[0]["data"])
                        net_done = True

                    if (not need_cookie or cookie_done) and (not need_net or net_done):
                        break

                    time.sleep(1)

            except ImportError as e:
                messagebox.showerror(
                    "依赖缺失",
                    f"缺少依赖库，请安装后重试: {e}\n\n",
                )
            except Exception as exc:
                logging.getLogger(__name__).exception("自动获取失败")
                messagebox.showerror("错误", f"自动获取失败: {exc}")
            finally:
                if cdp_session:
                    try:
                        cdp_session.close()
                    except Exception:
                        pass
                if driver:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                if server:
                    try:
                        server.shutdown()
                    except Exception:
                        pass
                if ext_dir:
                    import shutil

                    shutil.rmtree(ext_dir, ignore_errors=True)

        threading.Thread(target=_do, daemon=True).start()

    def auto_fetch_task_ids(self) -> None:
        ok = messagebox.askokcancel(
            "自动获取任务ID",
            "点击确定后会打开浏览器，请在 2 分钟内：\n\n"
            "打开有当前任务的直播间即可自动获取，\n"
            "或手动点击页面上的「刷新任务」按钮。\n\n"
            "捕获成功后浏览器会自动关闭。",
        )
        if not ok:
            return

        def on_match(data):
            if data.get("code") != 0:
                raise ValueError("response code != 0")
            tasks = data.get("data", {}).get("list", [])
            task_ids = [t.get("task_id") for t in tasks if t.get("task_id")]
            if not task_ids:
                raise ValueError("empty task list")
            self.task_ids_var.set(",".join(task_ids))
            logging.getLogger(__name__).info("任务ID获取成功: %s", ",".join(task_ids))

        self._browser_sniff(
            "/x/task/totalv2",
            "已打开浏览器，请打开有当前任务的直播间或点击刷新任务",
            on_network_match=on_match,
        )

    def auto_fetch_cookie(self) -> None:
        ok = messagebox.askokcancel(
            "自动获取Cookie",
            "点击确定后会打开浏览器，请在浏览器中登录 B 站。\n\n"
            "登录成功后 Cookie 会自动获取（含 httpOnly 字段），\n"
            "浏览器会自动关闭。",
        )
        if not ok:
            return

        def on_cookies(cookies: list):
            cookie_str = "; ".join(
                f"{c['name']}={c['value']}" for c in cookies if c.get("name")
            )
            if not cookie_str:
                messagebox.showwarning("提示", "未获取到 Cookie，请确认已登录 B 站")
                return
            self.cookie_var.set(cookie_str)
            logging.getLogger(__name__).info("Cookie 获取成功")

        self._browser_sniff(
            None,
            "已打开浏览器，正在获取 Cookie…",
            on_cookies=on_cookies,
        )

    def _schedule_task_refresh(self) -> None:
        if (
            self._stop_signal_set
            or self.worker_thread is None
            or not self.worker_thread.is_alive()
        ):
            return
        self.refresh_tasks(manual=False)
        try:
            interval = int(self.task_interval_var.get().strip() or "30")
        except ValueError:
            interval = 30
        self.root.after(max(10, interval) * 1000, self._schedule_task_refresh)

    @staticmethod
    def _format_task_progress(progresses: list) -> str:
        if not progresses:
            return "无任务数据"

        _DURATION_RE = re.compile(r"^(.+?)\d+分钟$")
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
                lines.append(f"{prefix} (当前: {cur} 分钟)")
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
                    bar = "█" * filled + "░" * (bar_width - filled)
                    if task.is_completed:
                        status = " ✔ 完成"
                    else:
                        status = f" {pct:>3}%"
                    lines.append(f"  {bar} {target:>4}分{status}")
            else:
                task = tasks[0]
                target = int(float(task.limit_value))
                cur = int(float(task.cur_value))
                pct = min(100, int(cur / max(1, target) * 100))
                filled = int(bar_width * pct / 100)
                bar = "█" * filled + "░" * (bar_width - filled)
                if task.is_completed:
                    status = " ✔ 完成"
                else:
                    status = f" {pct:>3}%"
                lines.append(f"{task.task_name} ({cur}/{target})")
                lines.append(f"  {bar}{status}")
        return "\n".join(lines)

    def _schedule_config_sync(self) -> None:
        self._sync_config_to_miner()
        if (
            not self._stop_signal_set
            and self.worker_thread is not None
            and self.worker_thread.is_alive()
        ):
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
            title="加载配置文件",
            filetypes=[
                ("JSON 文件", "*.json"),
                ("所有文件", "*.*"),
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
            logging.getLogger(__name__).info("配置已加载: %s", path)
        except Exception as exc:
            messagebox.showerror("加载失败", str(exc))

    def save_config(self) -> None:
        path = filedialog.asksaveasfilename(
            title="保存配置文件",
            defaultextension=".json",
            filetypes=[
                ("JSON 文件", "*.json"),
                ("所有文件", "*.*"),
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
            logging.getLogger(__name__).info("配置已保存: %s", path)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))


def run_gui() -> int:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    app = MinerGUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0
