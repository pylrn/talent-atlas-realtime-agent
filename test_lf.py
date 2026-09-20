import logging
from opentelemetry import trace
from opentelemetry.trace import set_span_in_context

def test():
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("test_span") as span:
        ctx = span.get_span_context()
        print("OTel trace_id:", format(ctx.trace_id, "032x"))

test()
