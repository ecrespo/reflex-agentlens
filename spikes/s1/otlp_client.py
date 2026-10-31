# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "opentelemetry-sdk>=1.29",
#   "opentelemetry-exporter-otlp-proto-http>=1.29",
#   "opentelemetry-exporter-otlp-proto-grpc>=1.29",
# ]
# ///
"""Spike S1c client: export spans with the OTLP HTTP and gRPC exporters (Task P0-04.1).

Usage: uv run --script otlp_client.py --http URL --grpc HOST:PORT --spans 10000 [--seconds S]
With --seconds, keeps exporting --spans per exporter per round until the time is up (load mode).
Prints JSON with the number of spans sent per protocol.
"""

from __future__ import annotations

import argparse
import json
import time

from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter as GrpcExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter as HttpExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

PAYLOAD = "lorem ipsum dolor sit amet " * 20  # ~540 bytes of message-like content per span


def provider(exporter, name: str) -> TracerProvider:
    tp = TracerProvider(resource=Resource.create({"service.name": name}))
    tp.add_span_processor(
        BatchSpanProcessor(
            exporter, max_queue_size=100_000, max_export_batch_size=512, schedule_delay_millis=50
        )
    )
    return tp


def emit(tp: TracerProvider, n: int) -> None:
    tracer = tp.get_tracer("spike")
    for i in range(0, n, 20):
        with tracer.start_as_current_span("invoke_agent spike") as root:
            root.set_attribute("gen_ai.operation.name", "invoke_agent")
            for j in range(min(19, n - i - 1)):
                with tracer.start_as_current_span("chat gpt-4o-mini") as s:
                    s.set_attribute("gen_ai.request.model", "gpt-4o-mini")
                    s.set_attribute("gen_ai.usage.input_tokens", 100 + j)
                    s.set_attribute("gen_ai.prompt", PAYLOAD)
    tp.force_flush(30_000)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", required=True)
    ap.add_argument("--grpc", required=True)
    ap.add_argument("--spans", type=int, default=10_000)
    ap.add_argument("--seconds", type=float, default=0)
    args = ap.parse_args()
    http = provider(HttpExporter(endpoint=args.http, timeout=30), "spike-http")
    grpc = provider(GrpcExporter(endpoint=args.grpc, insecure=True, timeout=30), "spike-grpc")
    sent, t0, rounds = 0, time.monotonic(), 0
    while True:
        emit(http, args.spans)
        emit(grpc, args.spans)
        sent += args.spans
        rounds += 1
        if time.monotonic() - t0 >= args.seconds:
            break
    elapsed = time.monotonic() - t0
    http.shutdown()
    grpc.shutdown()
    print(
        json.dumps(
            {
                "sent_per_protocol": sent,
                "rounds": rounds,
                "seconds": round(elapsed, 1),
                "spans_per_s_total": round(2 * sent / elapsed),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
