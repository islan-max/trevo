from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger("trevo")


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": record.name,
            "request_id": getattr(record, "request_id", None),
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def email_hash(email: str) -> str:
    return hashlib.sha256(email.encode()).hexdigest()[:16]


def client_ip_hash(request: Request) -> str | None:
    ip = request.client.host if request.client else ""
    if not ip:
        return None
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


def audit_log(event: str, user_id: str | None, details: dict | None = None) -> None:
    entry = {
        "audit": True,
        "event": event,
        "user_id": user_id,
        "timestamp": datetime.now(UTC).isoformat(),
        **(details or {}),
    }
    logger.info(json.dumps(entry))
