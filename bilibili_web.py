"""Single-process WebUI entry point."""
import os

import uvicorn

from bilibili_drops_miner.web import create_app


if __name__ == "__main__":
    uvicorn.run(create_app(), host=os.getenv("WEB_HOST", "127.0.0.1"),
                port=int(os.getenv("WEB_PORT", "23333")), access_log=False,
                timeout_graceful_shutdown=10)
