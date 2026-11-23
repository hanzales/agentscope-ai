from typing import Any

from agentscope_ai.client import AgentScope


def _record(scope: AgentScope, span: Any, response: Any) -> None:
    model = getattr(response, "model", None)
    if model:
        span.set_attribute("gen_ai.response.model", str(model))
    response_id = getattr(response, "id", None)
    if response_id:
        span.set_attribute("gen_ai.response.id", str(response_id))
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    scope.record_usage(
        span,
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        cached_tokens=getattr(usage, "cache_read_input_tokens", None),
        source="anthropic-message",
    )


def traced_message(scope: AgentScope, client: Any, /, **kwargs: Any) -> Any:
    """Call ``Anthropic.messages.create`` without capturing message content."""
    requested_model = str(kwargs.get("model", "unknown"))
    with scope.model_call("anthropic", requested_model) as span:
        response = client.messages.create(**kwargs)
        _record(scope, span, response)
        return response


async def traced_async_message(scope: AgentScope, client: Any, /, **kwargs: Any) -> Any:
    """Call ``AsyncAnthropic.messages.create`` without capturing message content."""
    requested_model = str(kwargs.get("model", "unknown"))
    with scope.model_call("anthropic", requested_model) as span:
        response = await client.messages.create(**kwargs)
        _record(scope, span, response)
        return response
