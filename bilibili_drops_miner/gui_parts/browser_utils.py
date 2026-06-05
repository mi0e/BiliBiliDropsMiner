from __future__ import annotations

import os
import re
import shutil
import sys


def find_browser(name: str) -> bool:
    if sys.platform == "darwin":
        if name == "edge":
            paths = [
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ]
        else:
            paths = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
            ]
    elif sys.platform == "win32":
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
    else:
        if name == "edge":
            candidates = (
                "microsoft-edge",
                "microsoft-edge-stable",
                "/usr/bin/microsoft-edge",
                "/usr/bin/microsoft-edge-stable",
            )
        else:
            candidates = (
                "google-chrome",
                "google-chrome-stable",
                "chromium",
                "chromium-browser",
                "/usr/bin/google-chrome",
                "/usr/bin/chromium",
            )
        for candidate in candidates:
            if candidate.startswith("/"):
                if os.path.exists(candidate):
                    return True
            elif shutil.which(candidate):
                return True
        return False
    return any(os.path.exists(path) for path in paths)


def detect_default_browser() -> str | None:
    if sys.platform == "win32":
        progids: list[str] = []
        try:
            import winreg

            for scheme in ("https", "http"):
                try:
                    with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        rf"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\{scheme}\UserChoice",
                    ) as key:
                        progids.append(winreg.QueryValueEx(key, "ProgId")[0].lower())
                except OSError:
                    continue
        except Exception:
            progids = []

        for progid in progids:
            if "chrome" in progid and "edge" not in progid:
                return "chrome"
            if "edge" in progid or "msedge" in progid:
                return "edge"
        return None

    if sys.platform == "darwin":
        try:
            import subprocess

            result = subprocess.run(
                [
                    "defaults",
                    "read",
                    "com.apple.LaunchServices/com.apple.launchservices.secure",
                    "LSHandlers",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            text = result.stdout.lower()
            if "google.chrome" in text or "com.google.chrome" in text:
                return "chrome"
            if "microsoft edge" in text or "com.microsoft.edgemac" in text:
                return "edge"
        except Exception:
            pass
        return None

    for cmd in (
        ["xdg-settings", "get", "default-web-browser"],
        ["xdg-mime", "query", "default", "x-scheme-handler/https"],
    ):
        try:
            import subprocess

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            progid = result.stdout.strip().lower()
            if not progid:
                continue
            if "chrome" in progid or "chromium" in progid:
                return "chrome"
            if "edge" in progid or "microsoft-edge" in progid:
                return "edge"
        except Exception:
            continue
    return None


def available_browsers() -> list[str]:
    return [browser for browser in ("chrome", "edge") if find_browser(browser)]


def browser_label(browser: str) -> str:
    return {"chrome": "Google Chrome", "edge": "Microsoft Edge"}.get(
        browser, browser
    )


def browser_try_order(preferred: str | None) -> tuple[str, ...]:
    available = available_browsers()
    if not available:
        return ()
    if preferred and preferred in available:
        return (preferred, *[item for item in available if item != preferred])
    default = detect_default_browser()
    if default in available:
        return (default, *[item for item in available if item != default])
    return tuple(available)


def extract_room_id_from_live_url(text: str) -> int | None:
    if not text:
        return None
    for pattern in (
        r"https?://live\.bilibili\.com/blanc/(\d+)",
        r"https?://live\.bilibili\.com/(\d+)",
        r"live\.bilibili\.com/blanc/(\d+)",
        r"live\.bilibili\.com/(\d+)",
    ):
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            room_id = int(match.group(1))
        except Exception:
            continue
        if room_id > 0:
            return room_id
    return None
