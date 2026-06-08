import os
import sys
from dataclasses import dataclass


def _truthy(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _default_service_name(component: str | None = None) -> str:
    configured = (
        os.getenv("OTEL_SERVICE_NAME")
        or os.getenv("AWCP_SERVICE_NAME")
        or os.getenv("AWCP_OTEL_SERVICE_NAME")
    )
    if configured:
        return configured

    base = os.getenv("AWCP_OTEL_SERVICE_PREFIX")
    if base and component:
        return f"{base}-{component}"
    if base:
        return base
    if component:
        return component
    return os.path.splitext(os.path.basename(sys.argv[0]))[0] or "awcp"


@dataclass(frozen=True)
class ObservabilitySettings:
    enabled: bool
    service_name: str
    service_namespace: str | None
    environment: str | None
    otlp_endpoint: str | None
    otlp_insecure: bool
    traces_enabled: bool
    logs_enabled: bool
    metrics_enabled: bool
    metric_export_interval_ms: int


def load_settings(component: str | None = None) -> ObservabilitySettings:
    return ObservabilitySettings(
        enabled=_truthy(os.getenv("AWCP_OTEL_ENABLED"), True),
        service_name=_default_service_name(component),
        service_namespace=os.getenv("AWCP_SERVICE_NAMESPACE"),
        environment=os.getenv("AWCP_ENVIRONMENT") or os.getenv("ENVIRONMENT"),
        otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
        otlp_insecure=_truthy(os.getenv("OTEL_EXPORTER_OTLP_INSECURE"), True),
        traces_enabled=_truthy(os.getenv("AWCP_OTEL_TRACES_ENABLED"), True),
        logs_enabled=_truthy(os.getenv("AWCP_OTEL_LOGS_ENABLED"), True),
        metrics_enabled=_truthy(os.getenv("AWCP_OTEL_METRICS_ENABLED"), True),
        metric_export_interval_ms=int(
            os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "15000")
        ),
    )
