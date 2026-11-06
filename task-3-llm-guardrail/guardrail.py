"""Entry point: run the LLM streaming guardrail.

Configuration comes from the environment (see README). Invalid configuration exits 2 before the
proxy binds a port.
"""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

from llm_guardrail import logging_setup
from llm_guardrail.app import build_app
from llm_guardrail.config import ConfigError, GuardrailConfig


def main() -> int:
    logging_setup.configure()
    log = logging.getLogger("llm_guardrail")
    try:
        config = GuardrailConfig.from_env()
    except ConfigError as exc:
        log.error("refusing to start: %s", exc)
        return 2
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8100"))
    log.info("guardrail listening on http://%s:%d/v1/chat/completions -> %s", host, port, config.upstream_url)
    uvicorn.run(build_app(config), host=host, port=port, log_config=None, access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
