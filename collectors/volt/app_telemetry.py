from __future__ import annotations

import atexit
import logging
import os
from typing import Any

log = logging.getLogger(__name__)


class AppTelemetry:
    def __init__(self, default_service_name: str):
        self.enabled = False
        self._counters: dict[str, Any] = {}
        self._histograms: dict[str, Any] = {}
        self._provider: Any = None
        self._meter: Any = None

        if not telemetry_enabled():
            return

        try:
            from opentelemetry import metrics
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.resources import Resource
        except ImportError as exc:
            log.warning("OTEL metrics disabled; dependency missing: %s", exc)
            return

        service_name = os.getenv("OTEL_SERVICE_NAME", default_service_name)
        resource = Resource.create(
            {
                "service.name": service_name,
                "service.namespace": "house-sensors",
                "deployment.environment": os.getenv("DEPLOYMENT_ENVIRONMENT", "truenas"),
                "project": "house-sensors",
            }
        )
        interval_ms = int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "30000"))
        reader = PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=interval_ms)
        self._provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(self._provider)
        self._meter = metrics.get_meter(service_name)
        self.enabled = True
        atexit.register(self.shutdown)

    def count(self, name: str, amount: int = 1, attributes: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        try:
            counter = self._counters.get(name)
            if counter is None:
                counter = self._meter.create_counter(name)
                self._counters[name] = counter
            counter.add(amount, attributes or {})
        except Exception as exc:  # pragma: no cover - telemetry must not break work.
            log.debug("OTEL counter failed: %s", exc)

    def record(self, name: str, value: float, attributes: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        try:
            histogram = self._histograms.get(name)
            if histogram is None:
                histogram = self._meter.create_histogram(name)
                self._histograms[name] = histogram
            histogram.record(value, attributes or {})
        except Exception as exc:  # pragma: no cover - telemetry must not break work.
            log.debug("OTEL histogram failed: %s", exc)

    def shutdown(self) -> None:
        if self._provider is not None:
            self._provider.shutdown()
            self._provider = None


def telemetry_enabled() -> bool:
    if os.getenv("OTEL_SDK_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    if os.getenv("OTEL_METRICS_EXPORTER", "").strip().lower() != "otlp":
        return False
    return bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"))


def telemetry_from_env(default_service_name: str) -> AppTelemetry:
    return AppTelemetry(default_service_name)
