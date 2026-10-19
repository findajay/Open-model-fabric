"""Audit logging for configuration and security-relevant actions."""

from __future__ import annotations

import logging
from typing import Any

from omf.core.domain.models import AuditEntry
from omf.core.ports.metadata import AuditLogRepository

log = logging.getLogger("omf.audit")


class AuditService:
    def __init__(self, repository: AuditLogRepository) -> None:
        self._repo = repository

    async def record(
        self,
        *,
        actor: str,
        action: str,
        target: str,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        entry = AuditEntry(
            actor=actor, action=action, target=target, request_id=request_id, details=details or {}
        )
        log.info(
            "audit action=%s target=%s actor=%s request_id=%s", action, target, actor, request_id
        )
        await self._repo.append_audit(entry)

    async def list(self, limit: int = 100) -> list[AuditEntry]:
        return await self._repo.list_audit(limit=limit)
