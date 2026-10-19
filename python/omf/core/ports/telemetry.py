"""Telemetry provider port (OpenTelemetry-compatible vocabulary).

Attributes must never contain prompt or completion text unless the operator opts in
with ``security.log_prompt_content``; services only pass identifiers, sizes and timings.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

Attributes = Mapping[str, str | int | float | bool | None]


class TelemetryProvider(ABC):
    @abstractmethod
    def record_metric(
        self,
        name: str,
        value: float,
        *,
        unit: str = "",
        kind: str = "histogram",
        attributes: Attributes | None = None,
    ) -> None:
        """``kind`` is ``counter`` or ``histogram`` (OpenTelemetry instrument kinds)."""

    @abstractmethod
    def record_event(self, name: str, attributes: Attributes | None = None) -> None: ...

    @abstractmethod
    def record_trace(
        self,
        name: str,
        *,
        duration_ms: float,
        status: str = "ok",
        attributes: Attributes | None = None,
    ) -> None:
        """Record a completed span (used when a span's lifetime is not a Python block)."""

    @contextmanager
    def span(self, name: str, attributes: Attributes | None = None) -> Iterator[dict[str, Any]]:
        """Convenience wrapper that times a block and records it via ``record_trace``."""
        import time

        start = time.perf_counter()
        extra: dict[str, Any] = {}
        status = "ok"
        try:
            yield extra
        except BaseException:
            status = "error"
            raise
        finally:
            merged = {**(attributes or {}), **extra}
            self.record_trace(
                name,
                duration_ms=(time.perf_counter() - start) * 1000,
                status=status,
                attributes=merged,
            )

    def shutdown(self) -> None:
        return None
