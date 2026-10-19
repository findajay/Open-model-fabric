"""Provider health monitoring.

Combines *active* checks (a periodic background loop calling ``health_check``) with
*passive* signals (inference failures reported by the inference service). The router
consults the latest status to skip providers that are known to be down.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from omf.core.domain.errors import OMFError
from omf.core.domain.models import HealthStatus, ProviderHealth, utcnow
from omf.core.ports.telemetry import TelemetryProvider
from omf.core.services.provider_registry import ProviderRegistry, disabled_health

log = logging.getLogger(__name__)


class HealthMonitor:
    def __init__(
        self,
        registry: ProviderRegistry,
        telemetry: TelemetryProvider,
        *,
        interval_s: float = 30.0,
        failure_threshold: int = 3,
    ) -> None:
        self._registry = registry
        self._telemetry = telemetry
        self._interval = interval_s
        self._threshold = failure_threshold
        self._state: dict[str, ProviderHealth] = {}
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ queries

    def get(self, provider_id: str) -> ProviderHealth:
        definition = self._registry.get(provider_id)
        if not definition.enabled:
            return disabled_health(provider_id)
        return self._state.get(provider_id) or ProviderHealth(provider_id=provider_id)

    def all(self) -> list[ProviderHealth]:
        return [self.get(d.id) for d in self._registry.definitions()]

    def is_down(self, provider_id: str) -> bool:
        return self.get(provider_id).status in (HealthStatus.DOWN, HealthStatus.DISABLED)

    # ------------------------------------------------------------------ checks

    async def check(self, provider_id: str) -> ProviderHealth:
        definition = self._registry.get(provider_id)
        if not definition.enabled:
            return disabled_health(provider_id)
        previous = self._state.get(provider_id)
        try:
            async with self._registry.lease(provider_id) as adapter:
                result = await adapter.health_check()
        except OMFError as exc:
            result = ProviderHealth(
                provider_id=provider_id,
                status=HealthStatus.DOWN,
                message=exc.message,
                error_code=exc.code,
                checked_at=utcnow(),
            )
        failures = 0
        if result.status == HealthStatus.DOWN:
            failures = (previous.consecutive_failures if previous else 0) + 1
        result = result.model_copy(update={"consecutive_failures": failures})
        self._store(result)
        return result

    async def check_all(self) -> list[ProviderHealth]:
        ids = self._registry.enabled_ids()
        results = await asyncio.gather(*(self.check(i) for i in ids), return_exceptions=True)
        out: list[ProviderHealth] = []
        for provider_id, res in zip(ids, results, strict=True):
            if isinstance(res, BaseException):
                log.warning("health check for %s crashed: %r", provider_id, res)
                continue
            out.append(res)
        return out

    def record_success(self, provider_id: str, latency_ms: float) -> None:
        current = self._state.get(provider_id)
        if current is None or current.status != HealthStatus.UP:
            self._store(
                ProviderHealth(
                    provider_id=provider_id,
                    status=HealthStatus.UP,
                    latency_ms=latency_ms,
                    checked_at=utcnow(),
                    message="request succeeded",
                )
            )

    def record_failure(self, provider_id: str, error: OMFError) -> None:
        """Passive signal. Only connectivity-type (retryable) errors affect health."""
        if not error.retryable:
            return
        current = self._state.get(provider_id) or ProviderHealth(provider_id=provider_id)
        failures = current.consecutive_failures + 1
        status = HealthStatus.DOWN if failures >= self._threshold else HealthStatus.DEGRADED
        self._store(
            current.model_copy(
                update={
                    "status": status,
                    "consecutive_failures": failures,
                    "message": error.message,
                    "error_code": error.code,
                    "checked_at": utcnow(),
                }
            )
        )

    def _store(self, health: ProviderHealth) -> None:
        self._state[health.provider_id] = health
        self._telemetry.record_metric(
            "omf.provider.availability",
            1.0 if health.status == HealthStatus.UP else 0.0,
            kind="gauge",
            attributes={"provider_id": health.provider_id, "status": health.status.value},
        )

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._task is None and self._interval > 0:
            self._task = asyncio.create_task(self._loop(), name="omf-health-monitor")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.check_all()
            except Exception:  # never let the monitor die
                log.exception("health monitor iteration failed")
            await asyncio.sleep(self._interval)
