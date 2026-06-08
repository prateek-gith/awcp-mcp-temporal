import logging
from typing import Any

from awcp.observability.settings import load_settings


_CONFIGURED = False
_ENABLED = False


try:
    from opentelemetry import context, metrics, propagate, trace
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.instrumentation.aiohttp_client import (
        AioHttpClientInstrumentor,
    )
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
except ImportError:  # pragma: no cover - lets the app boot before deps are installed
    context = metrics = propagate = trace = None
    FastAPIInstrumentor = None


logger = logging.getLogger(__name__)


def configure_observability(
    component: str | None = None,
    fastapi_app: Any | None = None,
) -> None:
    """Configure OTel once per process.

    The function is intentionally safe to call from every entrypoint. If OTel
    dependencies are absent or AWCP_OTEL_ENABLED=false, it leaves the existing
    application behavior unchanged.
    """
    global _CONFIGURED, _ENABLED

    if _CONFIGURED:
        if fastapi_app is not None:
            _instrument_fastapi(fastapi_app)
        return

    settings = load_settings(component)
    _CONFIGURED = True

    if not settings.enabled:
        logger.info("OpenTelemetry disabled by AWCP_OTEL_ENABLED")
        return

    if trace is None:
        logger.warning("OpenTelemetry packages are not installed; observability disabled")
        return

    resource_attrs: dict[str, str] = {SERVICE_NAME: settings.service_name}
    if settings.service_namespace:
        resource_attrs["service.namespace"] = settings.service_namespace
    if settings.environment:
        resource_attrs[DEPLOYMENT_ENVIRONMENT] = settings.environment
    if component:
        resource_attrs["awcp.component"] = component

    resource = Resource.create(resource_attrs)

    if settings.otlp_endpoint and settings.traces_enabled:
        # HTTP exporters derive TLS from the URL scheme (http:// = plain).
        # Each signal type uses its own sub-path per the OTLP HTTP spec.
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=settings.otlp_endpoint.rstrip("/") + "/v1/traces",
                )
            )
        )
        trace.set_tracer_provider(tracer_provider)

    if settings.otlp_endpoint and settings.metrics_enabled:
        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(
                endpoint=settings.otlp_endpoint.rstrip("/") + "/v1/metrics",
            ),
            export_interval_millis=settings.metric_export_interval_ms,
        )
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))

    if settings.otlp_endpoint and settings.logs_enabled:
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(
                    endpoint=settings.otlp_endpoint.rstrip("/") + "/v1/logs",
                )
            )
        )
        handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
        logging.getLogger().addHandler(handler)
        LoggingInstrumentor().instrument(set_logging_format=False)

    RequestsInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    AioHttpClientInstrumentor().instrument()
    if fastapi_app is not None:
        _instrument_fastapi(fastapi_app)

    _ENABLED = True
    logger.info("OpenTelemetry configured for service=%s", settings.service_name)


def _instrument_fastapi(app: Any) -> None:
    if FastAPIInstrumentor is None:
        return
    if getattr(app.state, "awcp_otel_instrumented", False):
        return
    FastAPIInstrumentor.instrument_app(app)
    app.state.awcp_otel_instrumented = True


def get_tracer(name: str):
    if trace is None:
        return _NoopTracer()
    return trace.get_tracer(name)


def get_meter(name: str):
    if metrics is None:
        return _NoopMeter()
    return metrics.get_meter(name)


def inject_context() -> dict[str, str]:
    if propagate is None:
        return {}
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def extract_context(carrier: dict | None):
    if propagate is None:
        return None
    return propagate.extract(carrier or {})


def is_enabled() -> bool:
    return _ENABLED


class _NoopSpan:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def set_attribute(self, *_args, **_kwargs):
        return None

    def record_exception(self, *_args, **_kwargs):
        return None

    def set_status(self, *_args, **_kwargs):
        return None


class _NoopTracer:
    def start_as_current_span(self, *_args, **_kwargs):
        return _NoopSpan()


class _NoopInstrument:
    def add(self, *_args, **_kwargs):
        return None

    def record(self, *_args, **_kwargs):
        return None


class _NoopMeter:
    def create_counter(self, *_args, **_kwargs):
        return _NoopInstrument()

    def create_histogram(self, *_args, **_kwargs):
        return _NoopInstrument()
