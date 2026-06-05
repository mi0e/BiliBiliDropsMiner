from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class UpdateCheckResult:
    latest_version: str
    release_url: str


def normalize_version(value: str) -> str:
    return value.strip().lower().lstrip("v")


def should_check_update(app_version: str, update_channel: str) -> bool:
    if update_channel != "release":
        return False
    current_version = normalize_version(app_version)
    return bool(current_version) and not current_version.startswith("dev")


def parse_update_payload(
    payload: dict[str, Any],
    *,
    current_version: str,
    releases_url: str,
) -> UpdateCheckResult | None:
    latest_version = str(payload.get("tag_name") or "").strip()
    if not latest_version:
        return None
    if normalize_version(latest_version) == normalize_version(current_version):
        return None
    release_url = str(payload.get("html_url") or "").strip() or releases_url
    return UpdateCheckResult(latest_version=latest_version, release_url=release_url)


def check_latest_release(
    *,
    app_version: str,
    update_channel: str,
    latest_release_api: str,
    releases_url: str,
    timeout: float = 8,
) -> UpdateCheckResult | None:
    if not should_check_update(app_version, update_channel):
        return None

    try:
        request = urllib.request.Request(
            latest_release_api,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "BilibiliDropsMiner",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status >= 400:
                return None
            payload = json.loads(response.read(65536).decode("utf-8", errors="replace"))
        if not isinstance(payload, dict):
            return None
        return parse_update_payload(
            payload,
            current_version=app_version,
            releases_url=releases_url,
        )
    except Exception:
        return None

