"""Local, single-account WebUI. No Qt or browser automation dependencies."""
from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import qrcode
import qrcode.image.svg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from bilibili_drops_miner.client import BilibiliClient
from bilibili_drops_miner.client_parts.qr_login import QrLoginApi, QrLoginStatus, REQUIRED_LOGIN_COOKIE_NAMES
from bilibili_drops_miner.client_parts.task_discovery import fetch_live_task_groups
from bilibili_drops_miner.config import MAX_ROOM_COUNT, MAX_THREAD_COUNT, MinerConfig
from bilibili_drops_miner.miner import BilibiliWatchTimeMiner
from bilibili_drops_miner.utils import parse_cookie, parse_task_ids


def redact(text: str, cookie: str) -> str:
    for part in cookie.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and value and (name.lower() in {"sessdata", "bili_jct", "csrf", "token", "sendkey"} or len(value) >= 16):
            text = text.replace(value, "[已隐藏]")
    text = re.sub(r"https?://\S+", "[请求地址已隐藏]", text)
    return re.sub(r"(?i)(SESSDATA|bili_jct|csrf|token|sendkey)\s*[=:]\s*[^\s;,]+",
                  r"\1=[已隐藏]", text)


def redact_payload(value, cookie: str):
    if isinstance(value, str):
        return redact(value, cookie)
    if isinstance(value, list):
        return [redact_payload(item, cookie) for item in value]
    if isinstance(value, dict):
        return {key: redact_payload(item, cookie) for key, item in value.items()}
    return value


class WebLogHandler(logging.Handler):
    def __init__(self, state):
        super().__init__()
        self.state = state

    def emit(self, record):
        # Never include exception tracebacks or complete request URLs in the UI.
        self.state.event(redact(record.getMessage(), self.state.cookie))


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    room_ids: list[StrictInt] = Field(default_factory=list, max_length=MAX_ROOM_COUNT)
    thread_count: int = Field(
        default=1, ge=1, le=MAX_THREAD_COUNT, strict=True
    )
    reconnect_delay_seconds: int = Field(default=8, ge=1, le=300, strict=True)
    task_query_interval_seconds: int = Field(default=30, ge=10, le=3600, strict=True)


class GroupSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    groups: list[StrictInt] = Field(max_length=100)
    generation: str


class ManualCookie(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cookie: str = Field(min_length=1, max_length=16384)


class ManualTasks(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_ids: str = Field(max_length=16384)


class WebState:
    def __init__(self, directory: Path):
        self.directory = directory
        self.lock = threading.RLock()
        self.qr_lock = threading.Lock()
        self.event_lock = threading.Lock()
        self.settings = Settings()
        self.cookie = ""
        self.groups: list[dict] = []
        self.selected: list[int] = []
        self.manual_task_ids: list[str] = []
        self.progress_context = None
        self.progress_items: list[dict] = []
        self.progress_version: str | None = None
        self.room_cache: tuple[int, str, float] | None = None
        self.generation = secrets.token_hex(16)
        self.thread: threading.Thread | None = None
        self.miner: BilibiliWatchTimeMiner | None = None
        self.stop_requested = threading.Event()
        self.phase = "stopped"
        self.events: deque[str] = deque(maxlen=200)
        self.qr: QrLoginApi | None = None
        self.qr_key = ""
        self.qr_id = ""
        self.qr_deadline = 0.0
        self.qr_last_poll = 0.0
        self.closing = False
        self.discovering = False
        self.task_busy = False
        path = directory / "web-state.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.settings = Settings.model_validate(data["settings"])
                self.check_rooms(self.settings.room_ids)
                self.cookie = data["cookie"]
                self.groups = data["groups"]
                self.selected = data["selected"]
                self.manual_task_ids = data.get("manual_task_ids", [])
                if not isinstance(self.manual_task_ids, list) or not all(
                    isinstance(task_id, str) for task_id in self.manual_task_ids
                ):
                    raise ValueError("Invalid manual task IDs")
                self.task_ids()
            except (ValueError, KeyError, TypeError, IndexError):
                raise RuntimeError("WebUI 数据文件无效，请检查 web-state.json") from None

    @staticmethod
    def check_rooms(rooms: list[int]) -> None:
        # 这里抛 ValueError 而不是 HTTPException：加载路径（WebState.__init__）
        # 用 except (ValueError, KeyError, TypeError, IndexError) 捕获异常并转成
        # 「数据文件无效」的提示，HTTPException 不在其中，会直接穿透未捕获。
        if any(room <= 0 or room > 10**15 for room in rooms):
            raise ValueError("房间号必须是有效的正整数")
        if len(set(rooms)) != len(rooms):
            raise ValueError("房间号不能重复")

    @classmethod
    def require_rooms(cls, rooms: list[int]) -> None:
        """check_rooms 的请求处理入口：把错误信息转成 HTTP 400。"""
        try:
            cls.check_rooms(rooms)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    def event(self, message: str) -> None:
        with self.event_lock:
            self.events.append(time.strftime("%H:%M:%S ") + message)

    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def require_idle(self) -> None:
        if self.closing or self.busy() or self.discovering or self.task_busy:
            raise HTTPException(409, "请等待当前操作结束，或先停止挂机")

    def require_task_edit(self) -> None:
        if self.closing or self.discovering or self.task_busy or self.phase == "stopping":
            raise HTTPException(409, "请等待当前任务操作或停止操作结束")

    def sync_running_tasks(self) -> None:
        if self.miner is not None:
            # Replace the list; in-flight queries retain their original ID snapshot.
            self.miner.config.task_ids = self.task_ids()

    def receive_miner_progress(self, miner, items, queried_ids) -> None:
        with self.lock:
            if miner is not self.miner or list(queried_ids) != self.task_ids():
                return
            self.publish_progress([asdict(item) for item in items], self.task_context())

    def task_ids(self) -> list[str]:
        selected_ids = [str(task) for i in self.selected
                        for task in self.groups[i]["task_ids"]]
        return list(dict.fromkeys(self.manual_task_ids + selected_ids))

    def save(self) -> None:
        # Only the WebUI's own state is written; desktop config files are untouched.
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / "web-state.json"
        temporary = self.directory / "web-state.json.tmp"
        payload = dict(settings=self.settings.model_dump(), cookie=self.cookie,
                       groups=self.groups, selected=self.selected,
                       manual_task_ids=self.manual_task_ids)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
        temporary.replace(path)

    def task_context(self) -> tuple:
        # Called under self.lock; results belong to this account and task selection.
        return (self.cookie, tuple(self.task_ids()), tuple(self.settings.room_ids), self.generation)

    def publish_progress(self, items: list[dict], context: tuple) -> None:
        with self.lock:
            if self.closing or context != self.task_context():
                return
            self.progress_items = redact_payload(items, self.cookie)
            self.progress_context = context
            self.progress_version = secrets.token_hex(16)

    def snapshot(self) -> dict:
        with self.lock, self.event_lock:
            progress_current = self.progress_context == self.task_context()
            return dict(settings=self.settings.model_dump(), logged_in=bool(self.cookie),
                        phase=self.phase, groups=self.groups, selected=self.selected,
                        manual_task_ids=self.manual_task_ids,
                        task_progress=self.progress_items if progress_current else [],
                        progress_version=self.progress_version if progress_current else None,
                        active_sessions=self.miner.active_session_count if self.miner else 0,
                        planned_sessions=len(self.settings.room_ids) * self.settings.thread_count,
                        generation=self.generation, logs=list(self.events))

    def close_qr(self) -> None:
        if self.qr is not None:
            self.qr.close()
        self.qr = None
        self.qr_key = ""
        self.qr_id = ""

    def start(self) -> None:
        with self.lock:
            self.require_idle()
            config = MinerConfig(cookie=self.cookie, task_ids=self.task_ids(),
                                 **self.settings.model_dump())
            try:
                config.validate()
            except ValueError:
                raise HTTPException(400, "请先扫码或填写 Cookie，并填写房间号") from None
            self.miner = BilibiliWatchTimeMiner(config)
            miner = self.miner
            miner.on_task_progress = lambda items, ids: self.receive_miner_progress(miner, items, ids)
            self.stop_requested.clear()
            self.phase = "starting"
            self.thread = threading.Thread(target=self._run, daemon=True, name="web-miner")
            self.thread.start()

    def _run(self) -> None:
        miner = self.miner
        assert miner is not None

        def run_miner() -> None:
            try:
                miner.run()
            except Exception:
                self.event("挂机退出：登录验证或网络请求失败，请检查账号状态后重试")
        worker = threading.Thread(target=run_miner, daemon=True, name="web-miner-owner")
        worker.start()
        self.event("正在启动挂机")
        while worker.is_alive():
            # Repeat stop until the owner exits: run() clears its stop event on entry.
            if self.stop_requested.is_set():
                miner.stop()
            with self.lock:
                if not self.stop_requested.is_set() and miner.uid is not None:
                    self.phase = "running"
            worker.join(0.1)
        with self.lock:
            if miner.login_invalidated:
                self.cookie = ""
                try:
                    self.save()
                except OSError:
                    self.event("清除失效凭据时写入失败，请检查数据目录权限")
                self.event("登录已失效，请重新扫码或填写 Cookie")
            self.phase = "stopped"
            self.event("挂机已停止，连接已释放")

    def stop(self) -> None:
        with self.lock:
            if self.busy():
                self.phase = "stopping"
                self.stop_requested.set()
                if self.miner:
                    self.miner.stop()

    def shutdown(self) -> None:
        with self.lock:
            self.closing = True
        self.stop()
        if self.thread:
            self.thread.join(timeout=25)
            if self.thread.is_alive():
                self.event("等待连接释放超时，进程退出时将结束剩余连接")
        with self.qr_lock:
            self.close_qr()


def create_app(*, data_dir: Path | None = None, password: str | None = None) -> FastAPI:
    password = password if password is not None else os.getenv("WEB_PASSWORD", "")
    state = WebState(data_dir or Path(os.getenv("WEB_DATA_DIR", "web-data")))
    room_lookup_lock = asyncio.Lock()
    account_lookup_lock = asyncio.Lock()
    account_cache = None

    def authorize(request: Request):
        if password:
            scheme, _, encoded = request.headers.get("Authorization", "").partition(" ")
            username, supplied_password = "", ""
            if scheme.lower() == "basic":
                try:
                    decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
                    username, separator, supplied_password = decoded.partition(":")
                    if not separator:
                        username = ""
                except (ValueError, UnicodeError, binascii.Error):
                    pass
            valid_user = secrets.compare_digest(username.encode(), b"admin")
            valid_password = secrets.compare_digest(supplied_password.encode(), password.encode())
            if not (valid_user and valid_password):
                raise HTTPException(401, "管理密码错误", headers={
                    "WWW-Authenticate": 'Basic realm="WebUI", charset="UTF-8"',
                })
        if request.method == "POST" and request.headers.get("X-Web-Request") != "1":
            raise HTTPException(403, "请求来源无效")

    @asynccontextmanager
    async def lifespan(_app):
        logger = logging.getLogger("bilibili_drops_miner")
        old_level, old_propagate = logger.level, logger.propagate
        handler = WebLogHandler(state)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        try:
            yield
        finally:
            await asyncio.to_thread(state.shutdown)
            logger.removeHandler(handler)
            logger.setLevel(old_level)
            logger.propagate = old_propagate

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.web = state

    @app.middleware("http")
    async def headers(request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            response = JSONResponse({"detail": "操作失败，请检查网络或数据目录权限后重试"}, status_code=500)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _exc):
        # Do not echo submitted credentials or arbitrary input in error responses.
        return JSONResponse({"detail": "参数无效，请检查房间号和数值范围"}, status_code=422)

    @app.exception_handler(Exception)
    async def safe_error(_request, _exc):
        return JSONResponse({"detail": "操作失败，请检查网络或数据目录权限后重试"}, status_code=500)

    @app.get("/healthz")
    def health():
        return {"ok": not state.closing}

    @app.get("/", dependencies=[Depends(authorize)])
    def index():
        return FileResponse(Path(__file__).with_name("web_static") / "index.html")

    @app.get("/assets/{name}", dependencies=[Depends(authorize)])
    def asset(name: str):
        if name not in {"app.js", "style.css", "plus-jakarta-sans.ttf"}:
            raise HTTPException(404)
        return FileResponse(Path(__file__).with_name("web_static") / name)

    @app.get("/api/state", dependencies=[Depends(authorize)])
    def get_state():
        return state.snapshot()

    @app.get("/api/account", dependencies=[Depends(authorize)])
    async def account_status():
        nonlocal account_cache
        async with account_lookup_lock:
            with state.lock:
                cookie = state.cookie
            if not cookie:
                account_cache = None
                return {"status": "empty", "name": ""}
            if account_cache and account_cache[0] == cookie and account_cache[2] > time.monotonic():
                return account_cache[1]
            client = None
            try:
                client = BilibiliClient(cookie)
                uid, name = await asyncio.wait_for(client.get_self_info(), timeout=12)
                result = {"status": "valid" if uid else "invalid", "name": name if uid else ""}
            except Exception:
                result = {"status": "error", "name": ""}
            finally:
                if client is not None:
                    try:
                        await client.close()
                    except Exception:
                        pass
            with state.lock:
                if state.cookie != cookie:
                    return {"status": "checking", "name": ""}
            account_cache = (cookie, result, time.monotonic() + (30 if result["status"] == "error" else 60))
            return result

    @app.get("/api/room/{room_id}", dependencies=[Depends(authorize)])
    async def room_title(room_id: int):
        with state.lock:
            if room_id not in state.settings.room_ids:
                raise HTTPException(404, "请先填写房间号")
        async with room_lookup_lock:
            with state.lock:
                cached = state.room_cache
            if cached and cached[0] == room_id and cached[2] > time.monotonic():
                return {"room_id": room_id, "title": cached[1]}
            client = BilibiliClient("")
            try:
                title = await asyncio.wait_for(client.get_room_title(room_id), timeout=12)
            except Exception:
                title = ""
            finally:
                await client.close()
            with state.lock:
                state.room_cache = (room_id, title, time.monotonic() + (300 if title else 30))
            return {"room_id": room_id, "title": title}

    @app.post("/api/cookie", dependencies=[Depends(authorize)])
    def manual_cookie(body: ManualCookie):
        cookie = body.cookie.strip()
        values = parse_cookie(cookie)
        if "\r" in cookie or "\n" in cookie or any(
            not values.get(name, "").strip() for name in REQUIRED_LOGIN_COOKIE_NAMES
        ):
            raise HTTPException(400, "Cookie 需包含 SESSDATA、bili_jct 和 DedeUserID，且不能换行")
        with state.qr_lock, state.lock:
            state.require_idle()
            state.close_qr()
            previous_cookie = state.cookie
            state.cookie = cookie
            try:
                state.save()
            except OSError:
                state.cookie = previous_cookie
                raise
            state.event("手动 Cookie 已保存")
        return {"ok": True}

    @app.post("/api/task-ids", dependencies=[Depends(authorize)])
    def manual_tasks(body: ManualTasks):
        task_ids = list(dict.fromkeys(parse_task_ids(body.task_ids.replace("，", ","))))
        if len(task_ids) > 100 or any(len(task_id) > 256 for task_id in task_ids):
            raise HTTPException(400, "最多填写 100 个任务 ID，每个不超过 256 字符")
        with state.lock:
            state.require_task_edit()
            previous_ids = state.manual_task_ids
            state.manual_task_ids = task_ids
            try:
                state.save()
            except OSError:
                state.manual_task_ids = previous_ids
                raise
            state.sync_running_tasks()
        return {"ok": True, "task_ids": task_ids}

    @app.post("/api/settings", dependencies=[Depends(authorize)])
    def settings(body: Settings):
        state.require_rooms(body.room_ids)
        with state.lock:
            state.require_idle()
            previous = state.settings, state.groups, state.selected, state.generation
            if body.room_ids != state.settings.room_ids:
                state.groups, state.selected = [], []
                state.generation = secrets.token_hex(16)
            state.settings = body
            try:
                state.save()
            except OSError:
                (
                    state.settings,
                    state.groups,
                    state.selected,
                    state.generation,
                ) = previous
                raise
        return state.snapshot()

    @app.post("/api/qr", dependencies=[Depends(authorize)])
    def generate_qr():
        with state.qr_lock:
            with state.lock:
                state.require_idle()
            state.close_qr()
            try:
                state.qr = QrLoginApi()
                challenge = state.qr.generate()
                state.qr_key = challenge.key
                state.qr_id = secrets.token_hex(16)
                state.qr_deadline = time.monotonic() + 180
                state.qr_last_poll = 0
                buffer = io.BytesIO()
                qrcode.make(challenge.url, image_factory=qrcode.image.svg.SvgPathImage).save(buffer)
                return {"id": state.qr_id, "svg": buffer.getvalue().decode()}
            except Exception:
                state.close_qr()
                raise HTTPException(502, "生成二维码失败，请稍后重试") from None

    @app.post("/api/qr/{qr_id}/poll", dependencies=[Depends(authorize)])
    def poll_qr(qr_id: str):
        with state.qr_lock:
            if not state.qr or qr_id != state.qr_id:
                raise HTTPException(409, "二维码已更新，请重新生成")
            if time.monotonic() >= state.qr_deadline:
                state.close_qr()
                return {"status": "EXPIRED"}
            if time.monotonic() - state.qr_last_poll < 2:
                raise HTTPException(429, "请稍后查询")
            state.qr_last_poll = time.monotonic()
            try:
                result = state.qr.poll(state.qr_key)
            except Exception:
                raise HTTPException(502, "扫码状态查询失败，请稍后重试") from None
            if result.status is QrLoginStatus.SUCCESS:
                with state.lock:
                    state.require_idle()
                    previous_cookie = state.cookie
                    state.cookie = result.cookie
                    try:
                        state.save()
                    except OSError:
                        state.cookie = previous_cookie
                        raise
                    state.event("扫码登录成功")
                state.close_qr()
            elif result.status is QrLoginStatus.EXPIRED:
                state.close_qr()
            return {"status": result.status.name}

    @app.post("/api/logout", dependencies=[Depends(authorize)])
    def logout():
        with state.qr_lock, state.lock:
            state.require_idle()
            state.close_qr()
            previous_cookie = state.cookie
            state.cookie = ""
            try:
                state.save()
            except OSError:
                state.cookie = previous_cookie
                raise
        return {"ok": True}

    @app.post("/api/discover", dependencies=[Depends(authorize)])
    def discover():
        with state.lock:
            state.require_task_edit()
            rooms = list(state.settings.room_ids)
            if not rooms:
                raise HTTPException(400, "请先填写房间号")
            state.discovering = True
        try:
            groups = []
            for room in rooms:
                try:
                    found = fetch_live_task_groups(room)
                except Exception:
                    raise HTTPException(502, f"房间 {room} 静态页面获取失败，请稍后重试") from None
                groups.extend(dict(group, room_id=room) for group in found)
            with state.lock:
                if state.closing:
                    raise HTTPException(409, "服务正在关闭")
                previous = state.groups, state.selected, state.generation
                state.groups = groups
                state.selected = [i for i, group in enumerate(groups) if group.get("active")]
                state.generation = secrets.token_hex(16)
                try:
                    state.save()
                except OSError:
                    state.groups, state.selected, state.generation = previous
                    raise
                state.sync_running_tasks()
                state.event(f"静态 HTML 解析完成：发现 {len(groups)} 个任务分组")
            return state.snapshot()
        finally:
            with state.lock:
                state.discovering = False

    @app.post("/api/selection", dependencies=[Depends(authorize)])
    def select(body: GroupSelection):
        with state.lock:
            state.require_task_edit()
            if body.generation != state.generation:
                raise HTTPException(409, "任务分组已更新，请刷新页面")
            if any(i < 0 or i >= len(state.groups) for i in body.groups):
                raise HTTPException(400, "任务分组无效")
            previous = state.selected
            state.selected = list(dict.fromkeys(body.groups))
            try:
                state.save()
            except OSError:
                state.selected = previous
                raise
            state.sync_running_tasks()
        return {"ok": True}

    @app.post("/api/start", dependencies=[Depends(authorize)])
    def start():
        state.start()
        return {"ok": True}

    @app.post("/api/stop", dependencies=[Depends(authorize)])
    def stop():
        state.stop()
        return {"ok": True}

    @app.post("/api/tasks/{action}", dependencies=[Depends(authorize)])
    async def tasks(action: str):
        if action not in {"progress", "claim"}:
            raise HTTPException(404)
        with state.lock:
            if state.task_busy or state.discovering or state.closing:
                raise HTTPException(409, "任务操作正在执行，请稍候")
            if not state.cookie:
                raise HTTPException(400, "请先扫码或填写 Cookie")
            cookie, ids = state.cookie, state.task_ids()
            context = state.task_context()
            if not ids:
                raise HTTPException(400, "请先手动填写任务 ID，或解析并选择任务分组")
            state.task_busy = True
        client = None
        try:
            client = BilibiliClient(cookie)
            operation = (client.get_task_progress(ids) if action == "progress"
                         else client.receive_all_mission_rewards(ids))
            result = await asyncio.wait_for(operation, timeout=120)
            items = [asdict(item) for item in result]
            if action == "progress":
                state.publish_progress(items, context)
            for item in items:
                if action == "claim" and item["status"] == -1:
                    item["message"] = "领取请求失败，请稍后刷新确认结果"
            return redact_payload({"items": items}, cookie)
        except Exception:
            raise HTTPException(502, "任务操作失败或超时，请稍后刷新确认结果") from None
        finally:
            try:
                if client is not None:
                    await client.close()
            finally:
                with state.lock:
                    state.task_busy = False

    return app
