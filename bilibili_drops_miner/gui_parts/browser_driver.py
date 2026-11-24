from __future__ import annotations

import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable


class BrowserPreparationCancelled(Exception):
    """The user cancelled browser preparation."""


def manager_status(line: str) -> str | None:
    """Translate known stages without exposing paths, URLs or proxy credentials."""
    text = line.lower()
    if any(word in text for word in ("extracting", "unzip", "uncompress", "decompress")):
        return "正在解压并安装浏览器驱动…"
    if "downloading" in text and ("driver" in text or ".zip" in text):
        return "正在下载浏览器驱动，网络较慢时请耐心等待…"
    if ("driver" in text and "already in cache" in text) or "driver path:" in text:
        return "浏览器驱动已就绪…"
    if "required driver:" in text or "discovering versions" in text:
        return "正在检查匹配的驱动版本…"
    if "detected browser:" in text:
        return "已检测到浏览器，正在检查驱动…"
    return None


def run_manager(
    command: list[str],
    on_status: Callable[[str], None],
    cancel: threading.Event,
    *,
    timeout: float = 360,
) -> dict[str, str]:
    """Read MIXED output live; bound the whole operation and reap on cancellation."""
    if cancel.is_set():
        raise BrowserPreparationCancelled()
    messages: queue.Queue[tuple[str, str]] = queue.Queue()
    output: list[str] = []
    readers: list[threading.Thread] = []
    started = time.monotonic()
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )

    def read_stream(name, stream) -> None:
        for line in stream:
            messages.put((name, line))

    last_status = None

    def consume(name: str, line: str) -> None:
        nonlocal last_status
        if name == "stdout":
            output.append(line)
        else:
            status = manager_status(line)
            if status and status != last_status:
                last_status = status
                on_status(status)

    try:
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            reader = threading.Thread(target=read_stream, args=(name, stream), daemon=True)
            reader.start()
            readers.append(reader)
        while process.poll() is None:
            if cancel.is_set():
                raise BrowserPreparationCancelled()
            if time.monotonic() - started >= timeout:
                raise TimeoutError("浏览器驱动准备超时，请检查网络或代理设置后重试。")
            try:
                consume(*messages.get(timeout=0.1))
            except queue.Empty:
                pass
        for reader in readers:
            reader.join(timeout=2)
        while not messages.empty():
            consume(*messages.get_nowait())
        if cancel.is_set():
            raise BrowserPreparationCancelled()
        if process.returncode:
            raise RuntimeError("浏览器驱动准备失败，请检查网络或代理设置后重试。")
        try:
            result = json.loads("".join(output))
        except ValueError:
            raise RuntimeError("驱动管理程序返回了无效结果，请更新 Selenium 后重试。") from None
        if not isinstance(result, dict) or not result.get("driver_path"):
            raise RuntimeError("驱动管理程序未返回可用驱动，请检查网络后重试。")
        return result
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for reader in readers:
            reader.join(timeout=2)
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()


def prepare_driver(browser, options, on_status: Callable[[str], None], cancel: threading.Event):
    # Keep imports lazy: normal GUI startup does not require Selenium.
    from selenium import webdriver
    from selenium.webdriver.common.selenium_manager import SeleniumManager

    if cancel.is_set():
        raise BrowserPreparationCancelled()
    service = webdriver.EdgeService() if browser == "edge" else webdriver.ChromeService()
    if service.path:
        if not Path(service.path).is_file():
            raise RuntimeError("指定的浏览器驱动不存在，请检查驱动路径设置。")
        return service
    on_status("正在检查浏览器驱动，首次使用可能需要下载…")
    command = [
        str(SeleniumManager._get_binary()), "--browser", browser,
        "--language-binding", "python", "--output", "MIXED", "--trace",
        "--avoid-browser-download", "--avoid-stats",
    ]
    if options.binary_location:
        command.extend(["--browser-path", options.binary_location])
    if options.browser_version:
        command.extend(["--browser-version", str(options.browser_version)])
    proxy = options.proxy
    if proxy and (proxy.ssl_proxy or proxy.http_proxy):
        command.extend(["--proxy", proxy.ssl_proxy or proxy.http_proxy])
    result = run_manager(command, on_status, cancel)
    if not Path(result["driver_path"]).is_file():
        raise RuntimeError("下载完成后未找到浏览器驱动，请检查安全软件拦截或重试。")
    service.path = result["driver_path"]
    if result.get("browser_path"):
        options.binary_location = result["browser_path"]
        options.browser_version = None
    return service
