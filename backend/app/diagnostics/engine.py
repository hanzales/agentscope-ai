import hashlib
import json
from collections import defaultdict
from typing import Any

from app.models import Span


def safe_fingerprint(attrs: dict[str, Any]) -> str:
    safe = {
        k: v
        for k, v in attrs.items()
        if k in {"tool.name", "gen_ai.tool.name", "tool.arguments.fingerprint"}
    }
    return hashlib.sha256(json.dumps(safe, sort_keys=True).encode()).hexdigest()[:16]


def repeated_tool_calls(spans: list[Span], threshold: int = 2) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[Span]] = defaultdict(list)
    for span in spans:
        tool = span.attributes.get("gen_ai.tool.name") or span.attributes.get("tool.name")
        if tool:
            groups[(str(tool), safe_fingerprint(span.attributes))].append(span)
    return [
        {
            "rule_id": "AS001",
            "severity": "warning",
            "title": "Repeated tool calls",
            "description": (
                f"Tool {tool!r} was called {len(items)} times with the same safe fingerprint."
            ),
            "evidence": {
                "tool": tool,
                "count": len(items),
                "span_ids": [s.span_id for s in items],
                "classification": "direct",
            },
            "recommendation": (
                "Cache or deduplicate equivalent tool requests when application semantics allow it."
            ),
            "span_id": items[0].span_id,
        }
        for (tool, _), items in groups.items()
        if len(items) >= threshold
    ]


def critical_path_ms(spans: list[Span]) -> float:
    if not spans:
        return 0.0
    events: list[tuple[float, int]] = []
    origin = min(s.started_at for s in spans)
    for s in spans:
        events.append(((s.started_at - origin).total_seconds() * 1000, 1))
        events.append(((s.ended_at - origin).total_seconds() * 1000, -1))
    # Union of intervals: concurrency is counted once.
    active = 0
    last = 0.0
    total = 0.0
    for point, delta in sorted(events, key=lambda x: (x[0], -x[1])):
        if active > 0:
            total += point - last
        active += delta
        last = point
    return total
