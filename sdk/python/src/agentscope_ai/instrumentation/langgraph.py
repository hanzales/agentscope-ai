from typing import Any

from agentscope_ai.client import AgentScope


class TracedGraph:
    """Per-instance LangGraph wrapper that preserves OpenTelemetry context."""

    def __init__(self, scope: AgentScope, graph: Any, name: str):
        self._scope = scope
        self._graph = graph
        self._name = name

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        with self._scope.tracer.start_as_current_span(
            self._name,
            attributes={"agentscope.span.type": "agent", "agentscope.framework": "langgraph"},
        ):
            return self._graph.invoke(*args, **kwargs)

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        with self._scope.tracer.start_as_current_span(
            self._name,
            attributes={"agentscope.span.type": "agent", "agentscope.framework": "langgraph"},
        ):
            return await self._graph.ainvoke(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._graph, name)


def trace_graph(scope: AgentScope, graph: Any, name: str = "langgraph.run") -> TracedGraph:
    """Wrap one compiled graph without global callbacks or instrumentation side effects."""
    return TracedGraph(scope, graph, name)
