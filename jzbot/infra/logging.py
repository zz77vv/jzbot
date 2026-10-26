"""Redact credentials even when third-party exception tracebacks contain URLs."""

import logging
import re
from logging.handlers import RotatingFileHandler

TOKEN_PATTERN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")


def redact(text):
    return TOKEN_PATTERN.sub("<redacted>", str(text))


class RedactingFormatter(logging.Formatter):
    def __init__(self, secret=""):
        super().__init__("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        self.secret = secret

    def format(self, record):
        value = super().format(record)
        if self.secret:
            value = value.replace(self.secret, "<redacted>")
        return redact(value)


def configure_logging(path, secret):
    handler = RotatingFileHandler(
        path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(RedactingFormatter(secret))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
