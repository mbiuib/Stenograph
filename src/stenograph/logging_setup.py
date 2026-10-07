"""Minimal logging configuration shared by the CLI and the server."""

import logging
import sys


def setup(level: str = "INFO") -> None:
    """Configure root logging once, writing to stderr."""
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
        force=True,
    )
