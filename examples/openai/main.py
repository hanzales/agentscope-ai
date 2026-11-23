"""Optional real OpenAI example. Requires OPENAI_API_KEY and agentscope-ai[openai]."""

import os

from agentscope_ai import AgentScope
from agentscope_ai.instrumentation.openai import traced_response
from openai import OpenAI

scope = AgentScope(
    service_name="openai-example",
    endpoint=os.getenv("AGENTSCOPE_ENDPOINT", "http://localhost:8000"),
    api_key=os.environ["AGENTSCOPE_API_KEY"],
)
client = OpenAI()
response = traced_response(
    scope,
    client,
    model=os.getenv("OPENAI_MODEL", "gpt-5-mini"),
    input="Reply with one short sentence about observability.",
)
print(response.output_text)
scope.force_flush()
