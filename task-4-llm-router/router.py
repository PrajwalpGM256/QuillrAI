"""Entry point: run the LLM router.

Configuration comes from the environment (see README). Invalid configuration exits 2 before the
router binds a port.
"""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

from llm_router import logging_setup
from llm_router.app import build_app
from llm_router.config import ConfigError, RouterConfig


def main() -> int:
    logging_setup.configure()
    log = logging.getLogger("llm_router")
    try:
        config = RouterConfig.from_env()
    except ConfigError as exc:
        log.error("refusing to start: %s", exc)
        return 2
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8200"))
    log.info("router listening on http://%s:%d/v1/chat/completions (db %s)", host, port, config.db_path)
    uvicorn.run(build_app(config), host=host, port=port, log_config=None, access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
