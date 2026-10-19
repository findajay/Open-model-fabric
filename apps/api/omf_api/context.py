"""Request-scoped context (correlation id) shared by middleware, handlers and logs."""

from __future__ import annotations

import logging
import re
import uuid
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("omf_request_id", default=None)
_VALID_ID = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


def new_request_id(incoming: str | None) -> str:
    """Accept a well-formed client-supplied id (for end-to-end correlation) or mint one."""
    if incoming and _VALID_ID.match(incoming):
        return incoming
    return f"req_{uuid.uuid4().hex[:20]}"


def current_request_id() -> str | None:
    return request_id_var.get()


class RequestIdLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        return True
