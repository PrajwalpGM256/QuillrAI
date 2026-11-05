"""Entry point: run the MCP security gateway.

Configuration comes from the environment (see README). Invalid configuration exits non-zero
before the gateway binds a port.
"""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

from mcp_gateway import logging_setup
from mcp_gateway.app import build_app
from mcp_gateway.config import ConfigError, GatewayConfig


def main() -> int:
    logging_setup.configure()
    log = logging.getLogger("mcp_gateway")
    try:
        config = GatewayConfig.from_env()
    except ConfigError as exc:
        log.error("refusing to start: %s", exc)
        return 2
    host = os.environ.get("GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("GATEWAY_PORT", "8080"))
    log.info("gateway listening on http://%s:%d/mcp -> %s", host, port, config.downstream_url)
    uvicorn.run(build_app(config), host=host, port=port, log_config=None, access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
