from __future__ import annotations

import logging
import os
import sys

try:
    from colorama import just_fix_windows_console
except Exception:  # pragma: no cover
    def just_fix_windows_console() -> None:
        return None


RESET = "\x1b[0m"
LEVEL_COLORS = {
    logging.DEBUG: "\x1b[36m",
    logging.INFO: "\x1b[32m",
    logging.WARNING: "\x1b[33m",
    logging.ERROR: "\x1b[31m",
    logging.CRITICAL: "\x1b[35m",
}
LEVEL_LABEL = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO ",
    logging.WARNING: "WARN ",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "FATAL",
}


class PrettyFormatter(logging.Formatter):
    def __init__(self, *, verbose: bool, use_color: bool) -> None:
        if verbose:
            fmt = "%(asctime)s | %(levelname)s | %(threadName)-18s | %(name)s:%(lineno)d | %(message)s"
        else:
            fmt = "%(asctime)s | %(message)s"
        super().__init__(fmt=fmt, datefmt="%H:%M:%S")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        original_levelname = record.levelname
        label = LEVEL_LABEL.get(record.levelno, original_levelname[:5].upper())
        if self.use_color:
            color = LEVEL_COLORS.get(record.levelno, "")
            record.levelname = f"{color}{label}{RESET}"
        else:
            record.levelname = label
        try:
            return super().format(record)
        finally:
            record.levelname = original_levelname


def setup_logging(
    *,
    verbose: bool,
    no_color: bool = False,
    extra_handlers: list[logging.Handler] | None = None,
) -> None:
    just_fix_windows_console()
    use_color = (
        not no_color
        and not bool(os.environ.get("NO_COLOR"))
        and (getattr(sys.stdout, "isatty", lambda: False)() or getattr(sys.stderr, "isatty", lambda: False)())
    )

    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    root_logger.setLevel(logging.INFO)
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.DEBUG)
    formatter = PrettyFormatter(verbose=verbose, use_color=use_color)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    for handler in extra_handlers or []:
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)

    app_level = logging.DEBUG if verbose else logging.INFO
    logging.getLogger("bilibili_miner").setLevel(app_level)
    for logger_name in ("httpx", "httpcore", "websockets", "asyncio"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
