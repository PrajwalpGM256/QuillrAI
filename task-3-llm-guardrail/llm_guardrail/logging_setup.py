"""Logging bound to stderr.

On the stdio transport stdout carries JSON-RPC and nothing else, so every log record,
including the SDK's own, goes to stderr through a single root handler.
"""

from __future__ import annotations

import logging
import os
import sys

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure(level: str | None = None) -> None:
    """Install one stderr handler on the root logger, replacing any existing handlers."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(handler)
    root.setLevel((level or os.environ.get("LOG_LEVEL") or "INFO").upper())
    logging.captureWarnings(True)
