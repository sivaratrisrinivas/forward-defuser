"""Logging, OpenTelemetry tracing and Prometheus metrics."""

import json
import logging
import sys

from prometheus_client import Counter, Histogram

from defuser.config import get_settings

CHECKS = Counter("defuser_checks_total", "Completed checks", ["verdict", "cache_hit", "channel"])
CHECK_LATENCY = Histogram(
    "defuser_check_latency_seconds",
    "End-to-end check latency",
    buckets=(1, 2, 5, 10, 15, 20, 30, 45, 60, 120),
)
CHECK_COST = Histogram(
    "defuser_check_cost_usd", "LLM cost per check", buckets=(0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.25)
)
TOOL_CALLS = Counter("defuser_tool_calls_total", "Agent tool calls", ["tool", "ok"])
VERIFIER = Counter("defuser_verifier_total", "Verifier outcomes", ["result"])
WEBHOOKS = Counter("defuser_webhooks_total", "Webhook deliveries", ["channel", "outcome"])
BUDGET_REJECTS = Counter("defuser_budget_rejects_total", "Requests refused by budget", ["scope"])


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"severity": record.levelname, "message": record.getMessage(), "logger": record.name}
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        return json.dumps(out)


def setup_logging() -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    logging.basicConfig(level=get_settings().log_level, handlers=[h], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)


_tracer = None


def setup_tracing(service: str) -> None:
    global _tracer
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    endpoint = get_settings().otel_exporter_otlp_endpoint
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
        )
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("defuser")


def tracer():
    from opentelemetry import trace

    return _tracer or trace.get_tracer("defuser")
