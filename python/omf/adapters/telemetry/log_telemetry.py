"""Default telemetry: structured log lines plus a small in-memory aggregate.

No external collector is required. Metric/trace attributes are emitted as JSON on the
``omf.telemetry`` logger (DEBUG for metrics, INFO for events/traces). The in-memory
aggregate backs ``GET /api/v1/metrics``.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict, deque
from typing import Any

from omf.core.ports.telemetry import Attributes, TelemetryProvider

log = logging.getLogger("omf.telemetry")


class LoggingTelemetry(TelemetryProvider):
    def __init__(self, *, window: int = 1000) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = defaultdict(float)
        self._histograms: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=window))
        self._gauges: dict[str, float] = {}

    @staticmethod
    def _series(name: str, attributes: Attributes | None) -> str:
        if not attributes:
            return name
        keys = ("provider_id", "model", "status", "error_code")
        labels = ",".join(f"{k}={attributes[k]}" for k in keys if attributes.get(k) is not None)
        return f"{name}{{{labels}}}" if labels else name

    def record_metric(
        self,
        name: str,
        value: float,
        *,
        unit: str = "",
        kind: str = "histogram",
        attributes: Attributes | None = None,
    ) -> None:
        series = self._series(name, attributes)
        with self._lock:
            if kind == "counter":
                self._counters[series] += value
            elif kind == "gauge":
                self._gauges[series] = value
            else:
                self._histograms[series].append(value)
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                json.dumps(
                    {
                        "type": "metric",
                        "name": name,
                        "value": value,
                        "unit": unit,
                        "kind": kind,
                        "attributes": dict(attributes or {}),
                    },
                    default=str,
                )
            )

    def record_event(self, name: str, attributes: Attributes | None = None) -> None:
        log.info(
            json.dumps(
                {"type": "event", "name": name, "attributes": dict(attributes or {})}, default=str
            )
        )

    def record_trace(
        self,
        name: str,
        *,
        duration_ms: float,
        status: str = "ok",
        attributes: Attributes | None = None,
    ) -> None:
        log.info(
            json.dumps(
                {
                    "type": "span",
                    "name": name,
                    "duration_ms": round(duration_ms, 2),
                    "status": status,
                    "attributes": dict(attributes or {}),
                },
                default=str,
            )
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            hist = {}
            for series, values in self._histograms.items():
                ordered = sorted(values)
                if ordered:
                    hist[series] = {
                        "count": len(ordered),
                        "p50": ordered[len(ordered) // 2],
                        "p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
                        "max": ordered[-1],
                    }
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "histograms": hist,
            }


class CompositeTelemetry(TelemetryProvider):
    """Fan out to several telemetry providers (e.g. logging + OpenTelemetry)."""

    def __init__(self, providers: list[TelemetryProvider]) -> None:
        self.providers = providers

    def record_metric(
        self,
        name: str,
        value: float,
        *,
        unit: str = "",
        kind: str = "histogram",
        attributes: Attributes | None = None,
    ) -> None:
        for p in self.providers:
            p.record_metric(name, value, unit=unit, kind=kind, attributes=attributes)

    def record_event(self, name: str, attributes: Attributes | None = None) -> None:
        for p in self.providers:
            p.record_event(name, attributes)

    def record_trace(
        self,
        name: str,
        *,
        duration_ms: float,
        status: str = "ok",
        attributes: Attributes | None = None,
    ) -> None:
        for p in self.providers:
            p.record_trace(name, duration_ms=duration_ms, status=status, attributes=attributes)

    def shutdown(self) -> None:
        for p in self.providers:
            p.shutdown()
