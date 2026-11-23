from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


class OtlpError(ValueError):
    pass


def _value(value: dict[str, Any]) -> object:
    for key in ("stringValue", "boolValue", "intValue", "doubleValue", "bytesValue"):
        if key in value:
            return value[key]
    if "arrayValue" in value:
        return [_value(v) for v in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return {
            v["key"]: _value(v.get("value", {})) for v in value["kvlistValue"].get("values", [])
        }
    return None


def attributes(items: list[dict[str, Any]] | None) -> dict[str, object]:
    return {item["key"]: _value(item.get("value", {})) for item in items or [] if "key" in item}


def _hex_id(value: str, size: int, name: str) -> str:
    normalized = value.lower()
    if len(normalized) != size or any(c not in "0123456789abcdef" for c in normalized):
        raise OtlpError(f"{name} must be {size} lowercase or uppercase hexadecimal characters")
    return normalized


def _time(value: str, name: str) -> datetime:
    try:
        nanos = int(value)
        return datetime.fromtimestamp(nanos / 1_000_000_000, tz=UTC)
    except (TypeError, ValueError, OverflowError) as exc:
        raise OtlpError(f"invalid {name}") from exc


@dataclass(frozen=True)
class ParsedSpan:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    kind: str
    service_name: str
    started_at: datetime
    ended_at: datetime
    status: str
    attributes: dict[str, object]
    error: str | None


KINDS = {0: "UNSPECIFIED", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}


def _kind(value: object) -> str:
    if isinstance(value, str) and value.startswith("SPAN_KIND_"):
        return value.removeprefix("SPAN_KIND_")
    try:
        return KINDS.get(int(value or 0), str(value))
    except (TypeError, ValueError):
        return str(value)


def parse(payload: dict[str, Any], limit: int) -> list[ParsedSpan]:
    if not isinstance(payload.get("resourceSpans", []), list):
        raise OtlpError("resourceSpans must be an array")
    parsed: list[ParsedSpan] = []
    for rs in payload.get("resourceSpans", []):
        resource_attrs = attributes(rs.get("resource", {}).get("attributes"))
        service = str(resource_attrs.get("service.name", "unknown"))
        scope_spans = rs.get("scopeSpans", rs.get("instrumentationLibrarySpans", []))
        for scope in scope_spans:
            for span in scope.get("spans", []):
                if len(parsed) >= limit:
                    raise OtlpError(f"request exceeds span limit of {limit}")
                attrs = {**resource_attrs, **attributes(span.get("attributes"))}
                start = _time(span.get("startTimeUnixNano"), "startTimeUnixNano")
                end = _time(span.get("endTimeUnixNano"), "endTimeUnixNano")
                if end < start:
                    raise OtlpError("span end precedes start")
                status_obj = span.get("status", {})
                status = str(status_obj.get("code", "STATUS_CODE_UNSET")).removeprefix(
                    "STATUS_CODE_"
                )
                error = str(status_obj.get("message")) if status_obj.get("message") else None
                parent = span.get("parentSpanId") or None
                parsed.append(
                    ParsedSpan(
                        trace_id=_hex_id(str(span.get("traceId", "")), 32, "traceId"),
                        span_id=_hex_id(str(span.get("spanId", "")), 16, "spanId"),
                        parent_span_id=_hex_id(str(parent), 16, "parentSpanId") if parent else None,
                        name=str(span.get("name") or "unknown"),
                        kind=_kind(span.get("kind", 0)),
                        service_name=service,
                        started_at=start,
                        ended_at=end,
                        status=status,
                        attributes=attrs,
                        error=error,
                    )
                )
    return parsed
