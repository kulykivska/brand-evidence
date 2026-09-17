"""structlog JSON logging with secret masking."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any
from urllib.parse import urlsplit

import structlog

SECRET_KEYS = re.compile(
    r"(secret|token|password|api_key|apikey|access_key|credential|auth|cookie)", re.I
)
URL_CREDS = re.compile(r"(://)[^/@\s]+:[^/@\s]+@")
# Feed and webhook URLs carry their authority in the path or the query, so
# the whole URL is the secret and only its host may be shown.
CAPABILITY_URL = re.compile(
    r"https?://[^\s'\"]*(alerts/feeds|/hooks?/|[?&](?:token|key|api_key|secret)=)[^\s'\"]*",
    re.I,
)


def _scrub(value: Any) -> Any:
    """Mask secrets in a value, walking into dicts and lists."""
    if isinstance(value, str):
        if "://" not in value:
            return value
        value = CAPABILITY_URL.sub(_host_only, value)
        return URL_CREDS.sub(r"\1***:***@", value)
    if isinstance(value, dict):
        return {k: ("***" if SECRET_KEYS.search(str(k)) else _scrub(v)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_scrub(v) for v in value)
    return value


def _host_only(match: re.Match[str]) -> str:
    parts = urlsplit(match.group(0))
    return f"{parts.scheme}://{parts.netloc}/***"


def _mask(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key, value in list(event_dict.items()):
        if SECRET_KEYS.search(key):
            event_dict[key] = "***"
        else:
            event_dict[key] = _scrub(value)
    return event_dict


def configure_logging(level: str = "info") -> None:
    logging.basicConfig(level=level.upper(), stream=sys.stderr, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _mask,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
