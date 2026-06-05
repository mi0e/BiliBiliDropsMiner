from __future__ import annotations

import json
from typing import Any


def validate_nav_payload(payload: dict[str, Any]) -> None:
    if payload.get("code") not in (0, -101):
        raise ValueError(f"登录状态异常: {json.dumps(payload, ensure_ascii=False)}")


def parse_self_info(payload: dict[str, Any]) -> tuple[int | None, str]:
    data = payload.get("data") or {}
    is_login = bool(data.get("isLogin"))
    if not is_login:
        return None, ""
    uid = int(data.get("mid") or 0)
    uname = str(data.get("uname") or "")
    if uid <= 0:
        return None, uname
    return uid, uname
