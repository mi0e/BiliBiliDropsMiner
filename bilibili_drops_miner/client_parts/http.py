from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx


def is_rate_limited_payload(payload: dict[str, Any]) -> bool:
    code = str(payload.get("code") or "")
    message = str(payload.get("message") or "")
    return code in {"-702", "-509"} or "频率" in message or "频繁" in message


async def request_with_transient_retry(
    request_coro: Callable[[], Awaitable[httpx.Response]],
    *,
    method: str,
    url: str,
    logger: logging.Logger,
) -> httpx.Response:
    # 高并发时，x25Kn/live API 偶发 ConnectTimeout/ReadTimeout。
    # 对这类瞬时网络异常做短退避重试，避免单次抖动就打断会话。
    delays = (0.35, 0.8)
    attempt_total = len(delays) + 1
    for attempt in range(1, attempt_total + 1):
        try:
            return await request_coro()
        except (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.ConnectError,
            httpx.RemoteProtocolError,
        ) as exc:
            if attempt >= attempt_total:
                raise
            delay = delays[attempt - 1]
            logger.debug(
                "%s %s 网络瞬时异常(%s/%s): %s，%.2fs 后重试",
                method,
                url,
                attempt,
                attempt_total,
                type(exc).__name__,
                delay,
            )
            await asyncio.sleep(delay)

    raise RuntimeError("unreachable retry state")


async def signed_get_json(
    *,
    http: httpx.AsyncClient,
    sign_wbi: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    clear_wbi_cache: Callable[[], None],
    logger: logging.Logger,
    url: str,
    params: dict[str, Any],
    headers: dict[str, str] | None = None,
    follow_redirects: bool = False,
    retry_on_wbi_miss: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    retries = 2 if retry_on_wbi_miss else 1
    for retry in range(retries):
        signed_params = await sign_wbi(params)
        response = await request_with_transient_retry(
            lambda: http.get(
                url,
                params=signed_params,
                headers=headers,
                follow_redirects=follow_redirects,
            ),
            method="GET",
            url=url,
            logger=logger,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") == 0:
            return payload
        if retry == 0 and retry_on_wbi_miss:
            clear_wbi_cache()
    return payload


async def signed_post_json(
    *,
    http: httpx.AsyncClient,
    sign_wbi: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    clear_wbi_cache: Callable[[], None],
    logger: logging.Logger,
    url: str,
    params: dict[str, Any],
    body: dict[str, Any],
    headers: dict[str, str] | None = None,
    retry_on_wbi_miss: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    retries = 2 if retry_on_wbi_miss else 1
    for retry in range(retries):
        signed_params = await sign_wbi(params)
        response = await request_with_transient_retry(
            lambda: http.post(
                url,
                params=signed_params,
                json=body,
                headers=headers,
            ),
            method="POST",
            url=url,
            logger=logger,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") == 0:
            return payload
        if retry == 0 and retry_on_wbi_miss:
            clear_wbi_cache()
    return payload


async def signed_post_query_json(
    *,
    http: httpx.AsyncClient,
    sign_wbi: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    clear_wbi_cache: Callable[[], None],
    logger: logging.Logger,
    url: str,
    params: dict[str, Any],
    headers: dict[str, str] | None = None,
    follow_redirects: bool = False,
    retry_on_wbi_miss: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    retries = 2 if retry_on_wbi_miss else 1
    for retry in range(retries):
        signed_params = await sign_wbi(params)
        response = await request_with_transient_retry(
            lambda: http.post(
                url,
                params=signed_params,
                headers=headers,
                follow_redirects=follow_redirects,
            ),
            method="POST",
            url=url,
            logger=logger,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") == 0:
            return payload
        if retry == 0 and retry_on_wbi_miss:
            clear_wbi_cache()
    return payload


async def signed_post_form_json(
    *,
    http: httpx.AsyncClient,
    sign_wbi: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    clear_wbi_cache: Callable[[], None],
    logger: logging.Logger,
    url: str,
    params: dict[str, Any],
    body: dict[str, Any],
    headers: dict[str, str] | None = None,
    retry_on_wbi_miss: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    retries = 2 if retry_on_wbi_miss else 1
    for retry in range(retries):
        signed_params = await sign_wbi(params)
        response = await request_with_transient_retry(
            lambda: http.post(
                url,
                params=signed_params,
                data=body,
                headers=headers,
            ),
            method="POST",
            url=url,
            logger=logger,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") == 0:
            return payload
        if retry == 0 and retry_on_wbi_miss:
            clear_wbi_cache()
    return payload

