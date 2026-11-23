# AgentScope AI Python SDK

OpenTelemetry-native decorators for agent and tool execution. Prompt and completion capture is disabled by default.

```python
from agentscope_ai import AgentScope

scope = AgentScope(
    service_name="research-agent", endpoint="http://localhost:8000", api_key="asai_..."
)


@scope.trace_agent("research")
def research(question: str):
    with scope.model_call("demo", "simulated-model") as span:
        scope.record_usage(span, input_tokens=10, output_tokens=20, source="provider")
    return {"answer": "Example"}
```

