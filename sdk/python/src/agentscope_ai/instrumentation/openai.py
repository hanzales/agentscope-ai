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
    details = getattr(usage, "input_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) if details is not None else None
    scope.record_usage(
        span,
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        cached_tokens=cached,
        source="openai-response",
    )


def traced_response(scope: AgentScope, client: Any, /, **kwargs: Any) -> Any:
    """Call ``OpenAI.responses.create`` without capturing input or output content."""
    requested_model = str(kwargs.get("model", "unknown"))
    with scope.model_call("openai", requested_model) as span:
        response = client.responses.create(**kwargs)
        _record(scope, span, response)
        return response


async def traced_async_response(scope: AgentScope, client: Any, /, **kwargs: Any) -> Any:
    """Call ``AsyncOpenAI.responses.create`` without capturing input or output content."""
    requested_model = str(kwargs.get("model", "unknown"))
    with scope.model_call("openai", requested_model) as span:
        response = await client.responses.create(**kwargs)
        _record(scope, span, response)
        return response
