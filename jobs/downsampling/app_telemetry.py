from __future__ import annotations

import atexit
import logging
import os
import time
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
        session = _ingest_auth_session()
        exporter = OTLPMetricExporter(session=session) if session is not None else OTLPMetricExporter()
        reader = PeriodicExportingMetricReader(exporter, export_interval_millis=interval_ms)
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


class _CognitoClientCredentialsAuth:
    """requests auth callable that attaches a Cognito client_credentials (M2M)
    bearer token to OTLP export requests and refreshes it before expiry. Used to
    authenticate to the Ahara telemetry ingest gateway."""

    def __init__(self, client_id: str, client_secret: str, token_url: str, scope: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url
        self._scope = scope
        self._token: str | None = None
        self._expires_at = 0.0

    def _refresh(self) -> None:
        import requests

        data = {"grant_type": "client_credentials"}
        if self._scope:
            data["scope"] = self._scope
        resp = requests.post(
            self._token_url,
            data=data,
            auth=(self._client_id, self._client_secret),
            timeout=10,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        # Refresh a minute before expiry; floor the lifetime so a tiny/absent
        # expires_in still yields a sane cache window.
        self._expires_at = time.monotonic() + max(int(payload.get("expires_in", 3600)) - 60, 30)

    def __call__(self, request: Any) -> Any:
        if self._token is None or time.monotonic() >= self._expires_at:
            self._refresh()
        request.headers["Authorization"] = f"Bearer {self._token}"
        return request


def _ingest_auth_session() -> Any:
    """Build a requests.Session that authenticates OTLP export with a Cognito
    M2M token, or None when no ingest credentials are configured (local/dev)."""
    client_id = os.getenv("OBS_INGEST_CLIENT_ID", "").strip()
    client_secret = os.getenv("OBS_INGEST_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None

    try:
        import requests
    except ImportError as exc:  # pragma: no cover - requests ships with the OTLP http exporter.
        log.warning("OTEL ingest auth disabled; requests missing: %s", exc)
        return None

    token_url = os.getenv(
        "OBS_INGEST_TOKEN_URL", "https://auth.services.ahara.io/oauth2/token"
    ).strip()
    scope = os.getenv("OBS_INGEST_SCOPE", "observability/ingest").strip()

    session = requests.Session()
    session.auth = _CognitoClientCredentialsAuth(client_id, client_secret, token_url, scope)
    return session


def telemetry_enabled() -> bool:
    if os.getenv("OTEL_SDK_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    if os.getenv("OTEL_METRICS_EXPORTER", "").strip().lower() != "otlp":
        return False
    return bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"))


def telemetry_from_env(default_service_name: str) -> AppTelemetry:
    return AppTelemetry(default_service_name)
