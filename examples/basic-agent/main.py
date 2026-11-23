import os
import time
from agentscope_ai import AgentScope

scope = AgentScope(service_name="demo-agent", endpoint=os.getenv("AGENTSCOPE_ENDPOINT", "http://localhost:8000"), api_key=os.environ["AGENTSCOPE_API_KEY"])

@scope.trace_tool("search")
def search(term: str) -> list[str]:
    time.sleep(0.02)
    return [f"Demo result for {term}"]

@scope.trace_agent("research")
def research(question: str) -> str:
    search(question); search(question)
    with scope.model_call("demo", "simulated-model") as span:
        scope.record_usage(span, input_tokens=32, output_tokens=18, source="simulated-demo")
    return "Simulated response"

print(research("OpenTelemetry"))
scope.force_flush()

