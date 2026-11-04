"""Entry point: run the refund MCP server over stdio.

stdout carries JSON-RPC only. Logging is configured before anything else so that every record,
including the SDK's, goes to stderr.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from refund_server import logging_setup

HERE = Path(__file__).resolve().parent


def main() -> None:
    logging_setup.configure()
    from refund_server.tools import build_server

    customers_path = Path(os.environ.get("REFUND_CUSTOMERS_PATH", HERE / "data" / "customers.json"))
    db_path = Path(os.environ.get("REFUND_DB_PATH", HERE / ".state" / "refunds.db"))
    logging.getLogger(__name__).info("starting stdio server db=%s", db_path)
    build_server(customers_path, db_path).run("stdio")


if __name__ == "__main__":
    main()
