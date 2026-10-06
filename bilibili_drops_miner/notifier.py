from __future__ import annotations

import logging
from urllib.parse import parse_qs, unquote, urlparse

import httpx

LOGGER = logging.getLogger(__name__)


class _NativeNotifyTarget:
    def __init__(self, service_url: str) -> None:
        self.service_url = service_url
        self.parsed = urlparse(service_url)

    @classmethod
    def from_url(cls, service_url: str) -> _NativeNotifyTarget | None:
        parsed = urlparse(service_url)
        if parsed.scheme in {"gotify", "gotifys", "schan", "wecombot", "wxwork"}:
            return cls(service_url)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.netloc.lower() == "qyapi.weixin.qq.com"
            and parsed.path.rstrip("/") == "/cgi-bin/webhook/send"
            and parse_qs(parsed.query).get("key")
        ):
            return cls(service_url)
        return None

    def notify(self, title: str, body: str) -> bool:
        try:
            if self.parsed.scheme in {"gotify", "gotifys"}:
                return self._notify_gotify(title, body)
            if self.parsed.scheme == "schan":
                return self._notify_serverchan(title, body)
            if self.parsed.scheme == "wecombot" or self._is_wecombot_webhook():
                return self._notify_wecombot(title, body)
            if self.parsed.scheme == "wxwork":
                return self._notify_wxwork(title, body)
        except Exception as exc:
            LOGGER.warning("通知发送失败: %s", exc)
        return False

    def _notify_gotify(self, title: str, body: str) -> bool:
        path = self.parsed.path.strip("/")
        if not self.parsed.netloc or not path:
            raise ValueError("Gotify 通知 URL 缺少 host 或 token")

        path_prefix, _, token = path.rpartition("/")
        endpoint_path = f"/{path_prefix}/message" if path_prefix else "/message"
        scheme = "https" if self.parsed.scheme == "gotifys" else "http"
        endpoint = f"{scheme}://{self.parsed.netloc}{endpoint_path}"
        query = parse_qs(self.parsed.query)
        priority_value = query.get("priority", ["5"])[0].lower()
        priority = (
            int(priority_value)
            if priority_value.isdigit()
            else {"low": 1, "moderate": 4, "normal": 5, "high": 8}.get(
                priority_value,
                5,
            )
        )
        response = httpx.post(
            endpoint,
            headers={"X-Gotify-Key": unquote(token)},
            json={"title": title, "message": body, "priority": priority},
            timeout=10.0,
        )
        return self._is_success_response(response)

    def _notify_serverchan(self, title: str, body: str) -> bool:
        token = self.parsed.netloc or self.parsed.path.strip("/").split("/", 1)[0]
        if not token:
            raise ValueError("Server 酱通知 URL 缺少 SendKey")

        response = httpx.post(
            f"https://sctapi.ftqq.com/{unquote(token)}.send",
            data={"title": title, "desp": body},
            timeout=10.0,
        )
        return self._is_success_response(response)

    def _notify_wecombot(self, title: str, body: str) -> bool:
        if self._is_wecombot_webhook():
            key = parse_qs(self.parsed.query).get("key", [""])[0]
        else:
            key = self.parsed.netloc or self.parsed.path.strip("/").split("/", 1)[0]
        if not key:
            raise ValueError("企业微信群机器人通知 URL 缺少 key")

        response = httpx.post(
            f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={unquote(key)}",
            json={
                "msgtype": "text",
                "text": {"content": self._format_content(title, body)},
            },
            timeout=10.0,
        )
        return self._is_success_response(response)

    def _notify_wxwork(self, title: str, body: str) -> bool:
        parts = [unquote(part) for part in self.parsed.path.split("/") if part]
        if not self.parsed.netloc or len(parts) < 2:
            raise ValueError("企业微信应用通知 URL 需要 corpid/agentid/secret")

        corpid = unquote(self.parsed.netloc)
        agentid, secret = parts[0], parts[1]
        query = parse_qs(self.parsed.query)
        touser = query.get("to", query.get("touser", ["@all"]))[0]

        token_response = httpx.get(
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
            params={"corpid": corpid, "corpsecret": secret},
            timeout=10.0,
        )
        if token_response.status_code >= 400:
            # 只报状态码，不回显 URL：corpsecret 就在查询串里。用户只要把企业微信
            # 通知地址配错一次（应用停用、IP 白名单变更、corpid 敲错都会返回 4xx），
            # 密钥就会经 notify() 的日志明文显示出来，拿到后可换取 access_token，
            # 以该应用身份向所有成员发消息。
            raise RuntimeError(f"企业微信 gettoken 失败: HTTP {token_response.status_code}")
        token_payload = token_response.json()
        access_token = token_payload.get("access_token")
        if not access_token:
            raise ValueError(f"企业微信 access_token 获取失败: {token_payload}")

        response = httpx.post(
            "https://qyapi.weixin.qq.com/cgi-bin/message/send",
            params={"access_token": access_token},
            json={
                "touser": touser,
                "msgtype": "text",
                "agentid": int(agentid) if agentid.isdigit() else agentid,
                "text": {"content": self._format_content(title, body)},
                "safe": 0,
            },
            timeout=10.0,
        )
        return self._is_success_response(response)

    def _is_wecombot_webhook(self) -> bool:
        return (
            self.parsed.scheme in {"http", "https"}
            and self.parsed.netloc.lower() == "qyapi.weixin.qq.com"
            and self.parsed.path.rstrip("/") == "/cgi-bin/webhook/send"
        )

    @staticmethod
    def _format_content(title: str, body: str) -> str:
        return f"{title}\n\n{body}" if title else body

    @staticmethod
    def _is_success_response(response: httpx.Response) -> bool:
        if not response.is_success:
            LOGGER.warning("通知发送失败，HTTP 状态码: %s", response.status_code)
            return False
        try:
            payload = response.json()
        except ValueError:
            return True

        error_code = payload.get("errcode", payload.get("code"))
        if error_code not in (None, 0):
            LOGGER.warning("通知发送失败，响应: %s", payload)
            return False
        return True


class MultiPlatformNotifier:
    def __init__(self, service_urls: list[str] | None = None) -> None:
        self.service_urls: list[str] = []
        self._native_targets: list[_NativeNotifyTarget] = []
        self._apprise = None
        self._enabled = False
        self.update_urls(service_urls)

    def update_urls(self, service_urls: list[str] | None = None) -> None:
        self.service_urls = [
            url.strip() for url in (service_urls or []) if url.strip()
        ]
        self._native_targets = []
        self._apprise = None
        self._enabled = False
        if not self.service_urls:
            return

        apprise_urls = []
        for url in self.service_urls:
            target = _NativeNotifyTarget.from_url(url)
            if target:
                self._native_targets.append(target)
            else:
                apprise_urls.append(url)

        if self._native_targets:
            self._enabled = True

        if not apprise_urls:
            return

        try:
            import apprise  # type: ignore

            app = apprise.Apprise()
            for url in apprise_urls:
                app.add(url)
            self._apprise = app
            self._enabled = True
        except Exception as exc:
            LOGGER.warning("通知推送初始化失败: %s", exc)

    @property
    def enabled(self) -> bool:
        return self._enabled and (
            bool(self._native_targets) or self._apprise is not None
        )

    def notify(self, title: str, body: str) -> bool:
        if not self.enabled:
            return False
        sent = False
        for target in self._native_targets:
            sent = target.notify(title=title, body=body) or sent
        try:
            if self._apprise is not None:
                sent = bool(self._apprise.notify(title=title, body=body)) or sent
        except Exception as exc:
            LOGGER.warning("通知发送失败: %s", exc)
        return sent
