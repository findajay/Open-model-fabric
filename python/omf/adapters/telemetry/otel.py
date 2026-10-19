"""Optional OpenTelemetry exporter (``pip install 'open-model-fabric[otel]'``).

Maps the telemetry port onto OTel instruments: counters -> ``Counter``, histograms ->
``Histogram``, gauges -> ``Gauge``, traces -> spans with explicit start/end timestamps.
Exports via OTLP/HTTP when ``endpoint`` is set; tests inject in-memory readers/exporters.
"""

from __future__ import annotations

import time
from typing import Any

from omf.core.domain.errors import ConfigurationError
from omf.core.ports.telemetry import Attributes, TelemetryProvider


def _clean(attributes: Attributes | None) -> dict[str, Any]:
    return {k: v for k, v in (attributes or {}).items() if v is not None}


class OpenTelemetryTelemetry(TelemetryProvider):
    def __init__(
        self,
        *,
        service_name: str = "open-model-fabric",
        endpoint: str | None = None,
        metric_reader: Any = None,
        span_exporter: Any = None,
    ) -> None:
        try:
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ConfigurationError("OpenTelemetry export needs the 'otel' extra") from exc

        resource = Resource.create({"service.name": service_name})
        if metric_reader is None and endpoint:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

            metric_reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics")
            )
        self._meter_provider = MeterProvider(
            resource=resource, metric_readers=[metric_reader] if metric_reader else []
        )
        self._tracer_provider = TracerProvider(resource=resource)
        if span_exporter is not None:
            self._tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
        elif endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            self._tracer_provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
            )
        self._meter = self._meter_provider.get_meter("omf")
        self._tracer = self._tracer_provider.get_tracer("omf")
        self._instruments: dict[tuple[str, str], Any] = {}

    def _instrument(self, name: str, kind: str, unit: str) -> Any:
        key = (name, kind)
        if key not in self._instruments:
            if kind == "counter":
                self._instruments[key] = self._meter.create_counter(name, unit=unit)
            elif kind == "gauge":
                self._instruments[key] = self._meter.create_gauge(name, unit=unit)
            else:
                self._instruments[key] = self._meter.create_histogram(name, unit=unit)
        return self._instruments[key]

    def record_metric(
        self,
        name: str,
        value: float,
        *,
        unit: str = "",
        kind: str = "histogram",
        attributes: Attributes | None = None,
    ) -> None:
        inst = self._instrument(name, kind, unit)
        attrs = _clean(attributes)
        if kind == "counter":
            inst.add(value, attrs)
        elif kind == "gauge":
            inst.set(value, attrs)
        else:
            inst.record(value, attrs)

    def record_event(self, name: str, attributes: Attributes | None = None) -> None:
        with self._tracer.start_as_current_span(name, attributes=_clean(attributes)):
            pass

    def record_trace(
        self,
        name: str,
        *,
        duration_ms: float,
        status: str = "ok",
        attributes: Attributes | None = None,
    ) -> None:
        from opentelemetry.trace import Status, StatusCode

        end = time.time_ns()
        start = end - int(duration_ms * 1_000_000)
        span = self._tracer.start_span(name, start_time=start, attributes=_clean(attributes))
        span.set_status(Status(StatusCode.OK if status == "ok" else StatusCode.ERROR))
        span.end(end_time=end)

    def shutdown(self) -> None:
        self._tracer_provider.shutdown()
        self._meter_provider.shutdown()
