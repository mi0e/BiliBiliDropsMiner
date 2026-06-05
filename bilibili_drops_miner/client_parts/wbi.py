from __future__ import annotations

import hashlib
import urllib.parse
from typing import Any

from bilibili_drops_miner.client_parts.constants import WBI_MIXIN_KEY_ENC_TAB


def get_mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return "".join(raw[index] for index in WBI_MIXIN_KEY_ENC_TAB)[:32]


def encode_query(params: dict[str, Any]) -> str:
    filtered = {
        key: "".join(ch for ch in str(value) if ch not in "!'()*")
        for key, value in params.items()
    }
    encoded_items = []
    for key, value in filtered.items():
        encoded_key = urllib.parse.quote(str(key), safe="")
        encoded_value = urllib.parse.quote(str(value), safe="")
        encoded_items.append(f"{encoded_key}={encoded_value}")
    return "&".join(encoded_items)


def parse_wbi_keys_from_nav(payload: dict[str, Any]) -> tuple[str, str]:
    data = payload.get("data") or {}
    wbi_img = data.get("wbi_img") or {}
    img_url = str(wbi_img.get("img_url", ""))
    sub_url = str(wbi_img.get("sub_url", ""))
    if not img_url or not sub_url:
        raise ValueError("nav 返回缺少 wbi_img")
    img_key = img_url.rsplit("/", 1)[-1].split(".", 1)[0]
    sub_key = sub_url.rsplit("/", 1)[-1].split(".", 1)[0]
    if not img_key or not sub_key:
        raise ValueError("wbi key 解析失败")
    return img_key, sub_key


def sign_wbi_params(
    params: dict[str, Any],
    *,
    img_key: str,
    sub_key: str,
    timestamp: int,
) -> dict[str, Any]:
    mixin_key = get_mixin_key(img_key, sub_key)
    signed = dict(params)
    signed["wts"] = timestamp
    sorted_items = dict(sorted(signed.items(), key=lambda item: item[0]))
    query = encode_query(sorted_items)
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return signed
