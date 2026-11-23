import atexit
import functools
import inspect
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any, ParamSpec, TypeVar

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.trace import Status, StatusCode, Tracer

P = ParamSpec("P")
R = TypeVar("R")


class PrivacyProcessor(SpanProcessor):
    def __init__(self, delegate: SpanProcessor, allowlist: set[str] | None, redact: set[str]):
        self.delegate, self.allowlist, self.redact = delegate, allowlist, redact

    def on_start(self, span: Span, parent_context: Any = None) -> None:
        self.delegate.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        self.delegate.on_end(span)

    def shutdown(self) -> None:
        self.delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.delegate.force_flush(timeout_millis)


class AgentScope:
    def __init__(
        self,
        service_name: str,
        endpoint: str,
        api_key: str | None = None,
        *,
        tracer_provider: TracerProvider | None = None,
        console_debug: bool = False,
        attribute_allowlist: set[str] | None = None,
        redact_attributes: set[str] | None = None,
    ):
        self._owned = tracer_provider is None
        self.provider = tracer_provider or TracerProvider(
            resource=Resource.create(
                {"service.name": service_name, "telemetry.sdk.language": "python"}
            )
        )
        headers = {"X-AgentScope-Key": api_key} if api_key else {}
        exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces", headers=headers)
        processor: SpanProcessor = BatchSpanProcessor(exporter)
        self.provider.add_span_processor(
            PrivacyProcessor(
                processor,
                attribute_allowlist,
                redact_attributes
                or {"password", "authorization", "api_key", "prompt", "completion"},
            )
        )
        if console_debug:
            self.provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        self.tracer: Tracer = self.provider.get_tracer("agentscope_ai", "0.1.0rc1")
        if self._owned:
            atexit.register(self.shutdown)

    def _decorator(self, kind: str, name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
        def decorate(fn: Callable[P, R]) -> Callable[P, R]:
            if getattr(fn, "__agentscope_instrumented__", False):
                return fn
            if inspect.iscoroutinefunction(fn):

                @functools.wraps(fn)
                async def async_wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
                    with self.tracer.start_as_current_span(
                        name, attributes={"agentscope.span.type": kind}
                    ) as span:
                        try:
                            return await fn(*args, **kwargs)  # type: ignore[misc]
                        except Exception as exc:
                            span.record_exception(exc)
                            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                            raise

                async_wrapped.__agentscope_instrumented__ = True
                return async_wrapped  # type: ignore[return-value]

            @functools.wraps(fn)
            def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
                with self.tracer.start_as_current_span(
                    name, attributes={"agentscope.span.type": kind}
                ) as span:
                    try:
                        return fn(*args, **kwargs)
                    except Exception as exc:
                        span.record_exception(exc)
                        span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                        raise

            wrapped.__agentscope_instrumented__ = True
            return wrapped

        return decorate

    def trace_agent(self, name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
        return self._decorator("agent", name)

    def trace_tool(self, name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
        return self._decorator("tool", name)

    @contextmanager
    def model_call(self, provider: str, model: str) -> Generator[Span, None, None]:
        with self.tracer.start_as_current_span(
            "model.call",
            attributes={
                "agentscope.span.type": "model",
                "gen_ai.provider.name": provider,
                "gen_ai.request.model": model,
            },
        ) as span:
            yield span

    def record_usage(
        self,
        span: Span,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cached_tokens: int | None = None,
        source: str,
    ) -> None:
        for key, value in (
            ("gen_ai.usage.input_tokens", input_tokens),
            ("gen_ai.usage.output_tokens", output_tokens),
            ("gen_ai.usage.cache_read.input_tokens", cached_tokens),
        ):
            if value is not None:
                if value < 0:
                    raise ValueError("token counts cannot be negative")
                span.set_attribute(key, value)
        span.set_attribute("agentscope.usage.source", source)

    def force_flush(self, timeout_millis: int = 10_000) -> bool:
        return self.provider.force_flush(timeout_millis)

    def shutdown(self) -> None:
        if self._owned:
            self.provider.shutdown()
