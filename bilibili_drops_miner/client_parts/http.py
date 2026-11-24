from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

# 请求 URL 里可能带凭据的参数。httpx 的 HTTPStatusError 消息固定包含完整的
# request.url，异常一旦被日志记录就会原样落盘，因此这里只打码这些参数的值，
# 其余部分保持原样以便排查。
#
#   csrf / csrf_token / bili_jct / sessdata    B 站登录凭据
#   access_key / access_token / token / sendkey  通知服务的凭据
#   key                           企业微信群机器人 webhook 的 key
#   qrcode_key                    扫码登录的轮询凭据
#   benchmark                     x25Kn 下发的签名根密钥，拿到即可伪造任意 seq_id
#                                 的心跳，即伪造观看时长
#   s                             x25Kn 的请求签名
#
# 值匹配到 &、; 或空白为止。; 不作为终止符是有意的：Cookie 形态
# （SESSDATA=a; bili_jct=b）里两项都是凭据，若在 ; 处截断，bili_jct 会漏网。
# 这里不要求键名前必须有 ? 或 &，宁可多打码也不漏——正文里的 token=xxx
# 被一并打码不影响排查。下划线属于单词字符，\b 不会在 access_token 内部
# 切出 token，所以带前缀的键名必须逐个列出。
_SENSITIVE_PARAM_RE = re.compile(
    r"(?i)\b"
    r"(csrf_token|csrf|bili_jct|sessdata|access_key|access_token|token|sendkey"
    r"|qrcode_key|key|benchmark|s)"
    r"=([^&;\s'\"]+)"
)


def sanitize_url(url: str) -> str:
    """打码文本中的敏感参数值，保留键名以便排查。"""
    return _SENSITIVE_PARAM_RE.sub(lambda m: f"{m.group(1)}=***", url)


def sanitize_exception_text(exc: BaseException) -> str:
    return sanitize_url(str(exc))


def raise_for_status(response: httpx.Response, method: str) -> None:
    """等价于 response.raise_for_status()，但异常消息里 URL 的敏感参数已打码。

    raise ... from None 切断异常链是必需的：exc_info=True 打印 traceback 时会
    连带输出原始异常，而原始 httpx 异常里是未脱敏的完整 URL。
    """
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise httpx.HTTPStatusError(
            f"{method} {sanitize_url(str(exc.request.url))} 返回 HTTP "
            f"{exc.response.status_code}",
            request=exc.request,
            response=exc.response,
        ) from None


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


async def signed_request_json(
    *,
    http: httpx.AsyncClient,
    sign_wbi: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    clear_wbi_cache: Callable[[], None],
    logger: logging.Logger,
    method: str,
    url: str,
    params: dict[str, Any],
    **request_kwargs: Any,
) -> dict[str, Any]:
    """WBI 签名请求：params 签名后作为查询串，request_kwargs（headers/json/data/
    follow_redirects）原样传给 httpx。非 0 响应最多刷新一次 WBI 密钥后重试。"""
    payload: dict[str, Any] = {}
    for retry in range(2):
        signed_params = await sign_wbi(params)
        response = await request_with_transient_retry(
            lambda: http.request(method, url, params=signed_params, **request_kwargs),
            method=method,
            url=url,
            logger=logger,
        )
        raise_for_status(response, method)
        payload = response.json()
        if payload.get("code") == 0:
            return payload
        # 限频需要由调用方按退避策略处理，不能在刷新 WBI 时立即重复请求。
        if retry > 0 or is_rate_limited_payload(payload):
            break
        clear_wbi_cache()
    return payload
