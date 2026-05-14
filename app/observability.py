"""Sentry + OpenTelemetry wiring.

Two layers, both safe to import in environments without credentials:

  1. **Sentry**       - error tracking and performance traces. Active when
                        `SENTRY_DSN` is set; otherwise a no-op.
  2. **OpenTelemetry**- distributed tracing of FastAPI requests, SQLAlchemy
                        queries, and outbound httpx calls. Active when either
                        `OTEL_EXPORTER_OTLP_ENDPOINT` is set (production
                        backend) or `OTEL_CONSOLE_EXPORTER=true` (dev).

Initialize once at module import time so spans cover lifespan + every request.
The lifespan handler in `main.py` calls `instrument_app(app, engine)` after the
app exists to attach instrumentation to the live objects.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from fastapi import FastAPI

from .config import get_settings

settings = get_settings()
logger = logging.getLogger("jaratrade.observability")

_sentry_inited = False
_otel_inited = False
_tracer_provider: Optional[Any] = None


# ───────────────────────── Sentry ─────────────────────────

def init_sentry() -> None:
    global _sentry_inited
    if _sentry_inited or not settings.sentry_dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        logger.warning("sentry-sdk not installed; skipping Sentry init")
        return

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        profiles_sample_rate=settings.sentry_profiles_sample_rate,
        send_default_pii=False,  # never send raw bodies/headers - PII risk
        integrations=[
            StarletteIntegration(transaction_style="endpoint"),
            FastApiIntegration(transaction_style="endpoint"),
            SqlalchemyIntegration(),
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
        release=os.getenv("RELEASE_VERSION"),
    )
    _sentry_inited = True
    logger.info("sentry initialised (env=%s, traces=%.2f)", settings.environment, settings.sentry_traces_sample_rate)


# ───────────────────────── OpenTelemetry ─────────────────────────

def _build_otel_exporter():
    """Pick the right exporter based on env. Returns None if neither is configured."""
    if settings.otel_console_exporter or (settings.environment == "development" and not settings.otel_exporter_otlp_endpoint):
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter
        return ConsoleSpanExporter()
    if settings.otel_exporter_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        headers = {}
        for pair in (settings.otel_exporter_otlp_headers or "").split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                headers[k.strip()] = v.strip()
        return OTLPSpanExporter(
            endpoint=settings.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces",
            headers=headers or None,
        )
    return None


def init_otel() -> None:
    global _otel_inited, _tracer_provider
    if _otel_inited:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning("opentelemetry-sdk not installed; skipping OTel init")
        return

    exporter = _build_otel_exporter()
    if exporter is None:
        # No exporter and not in dev console mode - leave OTel uninitialised
        return

    resource = Resource.create({
        "service.name": settings.otel_service_name,
        "service.version": os.getenv("RELEASE_VERSION", "dev"),
        "deployment.environment": settings.environment,
    })
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracer_provider = provider
    _otel_inited = True
    logger.info(
        "opentelemetry initialised (service=%s, exporter=%s)",
        settings.otel_service_name,
        type(exporter).__name__,
    )


def instrument_app(app: FastAPI, engine: Any | None = None) -> None:
    """Attach OTel instrumentations to the live app + DB engine.

    Called from main.lifespan after app + engine exist. Safe to call when OTel
    isn't initialised - the instrumentations will use the default (no-op)
    tracer provider.
    """
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.logging import LoggingInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    except ImportError:
        return

    # FastAPI - traces every HTTP request, hooks into our existing middleware
    try:
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health,docs,openapi.json,redoc")
    except Exception as e:  # noqa: BLE001
        logger.warning("FastAPI instrumentation failed: %r", e)

    if engine is not None:
        try:
            SQLAlchemyInstrumentor().instrument(engine=engine)
        except Exception as e:  # noqa: BLE001
            logger.warning("SQLAlchemy instrumentation failed: %r", e)

    try:
        HTTPXClientInstrumentor().instrument()
    except Exception as e:  # noqa: BLE001
        logger.warning("httpx instrumentation failed: %r", e)

    # Add trace_id/span_id to every log record
    try:
        LoggingInstrumentor().instrument(set_logging_format=False)
    except Exception as e:  # noqa: BLE001
        logger.warning("logging instrumentation failed: %r", e)


# ───────────────────────── Helpers ─────────────────────────

def get_tracer():
    """Get a tracer for ad-hoc spans. Falls back to the no-op tracer when OTel is off."""
    try:
        from opentelemetry import trace
        return trace.get_tracer("jaratrade.api")
    except ImportError:
        return None


def annotate_request(request_id: str) -> None:
    """Tag the current span + Sentry scope with our X-Request-ID for correlation."""
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        if span and span.is_recording():
            span.set_attribute("jaratrade.request_id", request_id)
    except ImportError:
        pass
    try:
        import sentry_sdk
        scope = sentry_sdk.get_current_scope()
        if scope:
            scope.set_tag("request_id", request_id)
    except ImportError:
        pass


# Initialise immediately so subsequent imports of FastAPI / instrumentations
# pick up the configured tracer provider.
init_sentry()
init_otel()
