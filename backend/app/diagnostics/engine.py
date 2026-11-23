import hashlib
import json
from collections import defaultdict
from statistics import median
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


def agent_bottleneck(spans: list[Span]) -> list[dict[str, Any]]:
    if not spans:
        return []
    path = critical_path_ms(spans)
    candidates = [
        span
        for span in spans
        if span.duration_ms >= 100 and path > 0 and span.duration_ms / path >= 0.5
    ]
    if not candidates:
        return []
    slowest = max(candidates, key=lambda span: span.duration_ms)
    share = round(slowest.duration_ms / path * 100, 1)
    return [
        {
            "rule_id": "AS002",
            "severity": "warning",
            "title": "Agent bottleneck",
            "description": (
                f"{slowest.operation_name!r} occupied {share}% of the trace critical path."
            ),
            "evidence": {
                "span_ids": [slowest.span_id],
                "span_duration_ms": round(slowest.duration_ms, 2),
                "critical_path_ms": round(path, 2),
                "critical_path_share_percent": share,
                "classification": "computed",
            },
            "recommendation": (
                "Inspect this operation for avoidable I/O, serialization, or sequential work."
            ),
            "span_id": slowest.span_id,
        }
    ]


def high_retry_count(spans: list[Span], threshold: int = 3) -> list[dict[str, Any]]:
    groups: dict[str, list[Span]] = defaultdict(list)
    for span in spans:
        attempt = span.attributes.get("retry.attempt") or span.attributes.get(
            "agentscope.retry.attempt"
        )
        operation = span.attributes.get("retry.operation") or span.attributes.get(
            "agentscope.retry.operation"
        )
        if attempt is not None and operation:
            groups[str(operation)].append(span)
    findings: list[dict[str, Any]] = []
    for operation, attempts in groups.items():
        max_attempt = max(
            int(
                span.attributes.get("retry.attempt")
                or span.attributes.get("agentscope.retry.attempt")
                or 0
            )
            for span in attempts
        )
        if max_attempt < threshold:
            continue
        failures = sum(1 for span in attempts if span.status == "ERROR")
        findings.append(
            {
                "rule_id": "AS004",
                "severity": "warning" if failures < max_attempt else "error",
                "title": "High retry count",
                "description": f"{operation!r} reached explicit retry attempt {max_attempt}.",
                "evidence": {
                    "operation": operation,
                    "max_attempt": max_attempt,
                    "failed_attempts": failures,
                    "span_ids": [span.span_id for span in attempts],
                    "classification": "direct",
                },
                "recommendation": (
                    "Review the upstream failure and cap retries with exponential backoff."
                ),
                "span_id": attempts[-1].span_id,
            }
        )
    return findings


def token_usage_spike(
    trace_id: str,
    spans: list[Span],
    historical_totals: list[int],
    minimum_samples: int = 10,
    multiplier: float = 2.0,
) -> list[dict[str, Any]]:
    current = sum((span.input_tokens or 0) + (span.output_tokens or 0) for span in spans)
    if current == 0 or len(historical_totals) < minimum_samples:
        return []
    baseline = median(historical_totals)
    if baseline <= 0 or current < baseline * multiplier:
        return []
    anchor = next(
        (span.span_id for span in spans if span.input_tokens is not None), spans[0].span_id
    )
    return [
        {
            "rule_id": "AS003",
            "severity": "warning",
            "title": "Token usage spike",
            "description": (
                f"Trace used {current} tokens versus a historical median of {baseline:g}."
            ),
            "evidence": {
                "trace_id": trace_id,
                "tokens": current,
                "baseline_median": baseline,
                "baseline_samples": len(historical_totals),
                "multiple": round(current / baseline, 2),
                "classification": "statistical",
            },
            "recommendation": (
                "Inspect prompt growth, retrieved context, and unusually long model output."
            ),
            "span_id": anchor,
        }
    ]
