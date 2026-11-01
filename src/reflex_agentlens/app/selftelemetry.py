"""Opt-in self-telemetry exporter (plan task P5-07.1; REQ-342.a/b/c, 323.a, DD-020, Art. 9).

Off by default (REQ-342.c). When ``[self_telemetry] enabled``, :func:`start` checks for a
self-loop, builds OTLP exporters (``http/protobuf`` or ``grpc``) for the spans and metrics of
Tech Design §7.2 with ``service.name=reflex-agentlens``, and installs an :class:`OtelRecorder` as
the process recorder (``core.telemetry``). The providers are private to agentlens: nothing is set
as the global OpenTelemetry provider, so a host's own instrumentation is untouched.

Loop check (REQ-342.b): the endpoint is refused (``ConfigError``, exit 2) when its host resolves
to a loopback or unspecified address, an address of this machine, or a bind address of this
instance, **and** its port is one this instance listens on for OTLP (HTTP, or gRPC when enabled).

The OpenTelemetry SDK is the ``[selftelemetry]`` extra; without it, enabling self-telemetry is a
configuration error naming the extra (exit 2).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from reflex_agentlens import __version__
from reflex_agentlens.core import telemetry
from reflex_agentlens.core.config import AgentlensConfig
from reflex_agentlens.core.errors import ConfigError

__all__ = ["EXTRA_HINT", "OtelRecorder", "SelfTelemetry", "check_loop", "start"]

log = logging.getLogger(__name__)

SERVICE_NAME = "reflex-agentlens"
EXTRA_HINT = "install the extra: uv add 'reflex-agentlens[selftelemetry]'"
_DEFAULT_PORTS = {"http": 80, "https": 443}

Resolver = Callable[[str, int], list[str]]


def _resolve(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        return []  # unresolvable now: the exporter will fail, never loop
    return [str(info[4][0]) for info in infos]


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        addr = ipaddress.ip_address(text.split("%", 1)[0].strip("[]"))
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _local_addresses(cfg: AgentlensConfig, resolve: Resolver) -> set[str]:
    server = cfg.server
    names = {server.http_host, server.grpc_host or server.http_host, socket.gethostname()}
    found: set[str] = set()
    for name in names:
        literal = _ip(name)
        addrs = [literal] if literal is not None else [_ip(a) for a in resolve(name, 0)]
        found.update(str(a) for a in addrs if a is not None)
    return found


def check_loop(cfg: AgentlensConfig, resolve: Resolver = _resolve) -> None:
    """REQ-342.b: refuse an endpoint that is this instance's own ingest endpoint."""
    endpoint = cfg.self_telemetry.endpoint
    if endpoint is None:
        return
    parts = urlsplit(str(endpoint))
    host = parts.hostname or ""
    port = parts.port or _DEFAULT_PORTS.get(parts.scheme, 80)
    server = cfg.server
    ours = {server.http_port} | ({server.grpc_port} if server.grpc_enabled else set())
    if port not in ours:
        return
    local = _local_addresses(cfg, resolve)
    literal = _ip(host)
    targets = [literal] if literal is not None else [_ip(a) for a in resolve(host, port)]
    for addr in targets:
        if addr is None:
            continue
        if addr.is_loopback or addr.is_unspecified or str(addr) in local:
            raise ConfigError(
                "self_telemetry.endpoint",
                "config",
                f"points at this instance's own ingest endpoint ({host}:{port}); "
                "self-telemetry must go to another collector (no self-loop)",
            )


# ── the recorder ─────────────────────────────────────────────────────────────────────────────


def _clean(keys: frozenset[str], labels: Mapping[str, str] | None) -> dict[str, str]:
    """Only declared keys, only declared values (anything else becomes ``other``)."""
    if not labels:
        return {}
    out: dict[str, str] = {}
    for key, value in labels.items():
        if key in keys:
            out[key] = value if value in telemetry.LABEL_VALUES.get(key, ()) else "other"
    return out


class OtelRecorder(telemetry.Recorder):
    """``core.telemetry.Recorder`` over an OpenTelemetry meter and tracer (thread-safe)."""

    def __init__(self, meter_provider: Any, tracer_provider: Any) -> None:
        meter = meter_provider.get_meter(SERVICE_NAME, __version__)
        self.tracer = tracer_provider.get_tracer(SERVICE_NAME, __version__)
        self._lock = threading.Lock()
        self._instruments: dict[str, tuple[Any, frozenset[str]]] = {}
        for name, (kind, unit, description, keys) in telemetry.METRICS.items():
            factory = {
                "counter": meter.create_counter,
                "histogram": meter.create_histogram,
                "gauge": meter.create_gauge,
            }[kind]
            self._instruments[name] = (factory(name, unit=unit, description=description), keys)

    def add(self, name: str, value: int = 1, labels: Mapping[str, str] | None = None) -> None:
        found = self._instruments.get(name)
        if found is not None and value:
            found[0].add(value, _clean(found[1], labels))

    def record(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        found = self._instruments.get(name)
        if found is not None:
            found[0].record(value, _clean(found[1], labels))

    def set(self, name: str, value: float) -> None:
        found = self._instruments.get(name)
        if found is not None:
            with self._lock:
                found[0].set(value)

    @contextmanager
    def span(self, name: str, attributes: dict[str, str] | None = None) -> Iterator[None]:
        keys = telemetry.SPANS.get(name)
        if keys is None:
            yield
            return
        with self.tracer.start_as_current_span(name, record_exception=False) as span:
            try:
                yield
            finally:
                for key, value in _clean(keys, attributes).items():
                    span.set_attribute(key, value)


# ── lifecycle ────────────────────────────────────────────────────────────────────────────────


@dataclass
class SelfTelemetry:
    meter_provider: Any
    tracer_provider: Any

    def shutdown(self) -> None:
        """Restore the no-op recorder and flush what is pending (best effort)."""
        telemetry.install(None)
        for provider in (self.tracer_provider, self.meter_provider):
            try:
                provider.shutdown()
            except Exception as exc:  # an unreachable collector must not fail the shutdown
                log.warning("self-telemetry shutdown failed (%s)", type(exc).__name__)


def _exporters(cfg: AgentlensConfig) -> tuple[Any, Any]:
    st = cfg.self_telemetry
    endpoint = str(st.endpoint).rstrip("/")
    headers: dict[str, str] = {}
    if st.headers is not None:
        from opentelemetry.util.re import parse_env_headers

        headers = dict(parse_env_headers(st.headers.get_secret_value(), liberal=True))
    if st.protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter as GrpcMetrics,
        )
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as GrpcSpans,
        )

        insecure = urlsplit(endpoint).scheme != "https"
        grpc_metrics = GrpcMetrics(endpoint=endpoint, insecure=insecure, headers=headers)
        return grpc_metrics, GrpcSpans(endpoint=endpoint, insecure=insecure, headers=headers)
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    metrics = OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics", headers=headers)
    return metrics, OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", headers=headers)


def start(cfg: AgentlensConfig, resolve: Resolver = _resolve) -> SelfTelemetry | None:
    """Install the exporter when enabled; ``None`` (and nothing installed) otherwise."""
    if not cfg.self_telemetry.enabled:
        return None
    check_loop(cfg, resolve)
    try:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        metric_exporter, span_exporter = _exporters(cfg)
    except ImportError:
        raise ConfigError(
            "self_telemetry.enabled",
            "config",
            f"self-telemetry needs the OpenTelemetry SDK; {EXTRA_HINT}",
        ) from None
    resource = Resource.create({"service.name": SERVICE_NAME, "service.version": __version__})
    meters = MeterProvider(
        resource=resource, metric_readers=[PeriodicExportingMetricReader(metric_exporter)]
    )
    tracers = TracerProvider(resource=resource)
    tracers.add_span_processor(BatchSpanProcessor(span_exporter))
    telemetry.install(OtelRecorder(meters, tracers))
    log.info("self-telemetry enabled (%s)", cfg.self_telemetry.protocol)
    return SelfTelemetry(meters, tracers)
