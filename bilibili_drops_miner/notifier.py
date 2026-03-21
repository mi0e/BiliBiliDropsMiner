from __future__ import annotations

import logging

LOGGER = logging.getLogger(__name__)


class MultiPlatformNotifier:
    def __init__(self, service_urls: list[str] | None = None) -> None:
        self.service_urls = [url.strip() for url in (service_urls or []) if url.strip()]
        self._apprise = None
        self._enabled = False
        if not self.service_urls:
            return
        try:
            import apprise  # type: ignore

            app = apprise.Apprise()
            for url in self.service_urls:
                app.add(url)
            self._apprise = app
            self._enabled = True
        except Exception as exc:
            LOGGER.warning("\u901a\u77e5\u63a8\u9001\u521d\u59cb\u5316\u5931\u8d25: %s", exc)

    def update_urls(self, service_urls: list[str] | None = None) -> None:
        self.service_urls = [
            url.strip() for url in (service_urls or []) if url.strip()
        ]
        self._apprise = None
        self._enabled = False
        if not self.service_urls:
            return
        try:
            import apprise  # type: ignore

            app = apprise.Apprise()
            for url in self.service_urls:
                app.add(url)
            self._apprise = app
            self._enabled = True
        except Exception as exc:
            LOGGER.warning("\u901a\u77e5\u63a8\u9001\u521d\u59cb\u5316\u5931\u8d25: %s", exc)

    @property
    def enabled(self) -> bool:
        return self._enabled and self._apprise is not None

    def notify(self, title: str, body: str) -> bool:
        if not self.enabled:
            return False
        try:
            return bool(self._apprise.notify(title=title, body=body))  # type: ignore[union-attr]
        except Exception as exc:
            LOGGER.warning("\u901a\u77e5\u53d1\u9001\u5931\u8d25: %s", exc)
            return False
