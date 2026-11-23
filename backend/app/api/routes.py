from datetime import UTC, datetime
from typing import Annotated, Any

from app.analytics.pricing import calculate
from app.core.config import get_settings
from app.core.security import hash_api_key, issue_api_key, verify_api_key
from app.diagnostics.engine import critical_path_ms, repeated_tool_calls
from app.ingestion.otlp import OtlpError, parse
from app.models import DiagnosticFinding, Project, Span, Trace
from app.storage.database import get_session
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()
SessionDep = Annotated[AsyncSession, Depends(get_session)]


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    service_identifier: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=120)


async def admin_auth(authorization: Annotated[str | None, Header()] = None) -> None:
    settings = get_settings()
    if settings.demo_mode:
        return
    expected = settings.admin_token
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if not expected or not secrets_compare(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid admin credentials")


def secrets_compare(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode(), b.encode())


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _protobuf_dict(message: ExportTraceServiceRequest) -> dict[str, Any]:
    """Convert protobuf while honoring OTLP's required hexadecimal ID JSON mapping."""
    payload = MessageToDict(message, preserving_proto_field_name=False)
    for resource_dict, resource_proto in zip(
        payload.get("resourceSpans", []), message.resource_spans, strict=True
    ):
        for scope_dict, scope_proto in zip(
            resource_dict.get("scopeSpans", []), resource_proto.scope_spans, strict=True
        ):
            for span_dict, span_proto in zip(
                scope_dict.get("spans", []), scope_proto.spans, strict=True
            ):
                span_dict["traceId"] = span_proto.trace_id.hex()
                span_dict["spanId"] = span_proto.span_id.hex()
                if span_proto.parent_span_id:
                    span_dict["parentSpanId"] = span_proto.parent_span_id.hex()
    return payload


async def project_for_key(session: AsyncSession, api_key: str | None) -> Project:
    if not api_key:
        raise HTTPException(status_code=401, detail="missing X-AgentScope-Key")
    # Hash lookup plus constant-time confirmation avoids storing plaintext credentials.
    digest = hash_api_key(api_key)
    project = await session.scalar(select(Project).where(Project.api_key_hash == digest))
    if project is None or not verify_api_key(api_key, project.api_key_hash):
        raise HTTPException(status_code=401, detail="invalid ingestion credential")
    return project


@router.post("/api/projects", status_code=201, dependencies=[Depends(admin_auth)])
async def create_project(data: ProjectCreate, session: SessionDep) -> dict[str, Any]:
    if await session.scalar(
        select(Project.id).where(Project.service_identifier == data.service_identifier)
    ):
        raise HTTPException(status_code=409, detail="service identifier already exists")
    key = issue_api_key()
    project = Project(
        name=data.name, service_identifier=data.service_identifier, api_key_hash=hash_api_key(key)
    )
    session.add(project)
    await session.commit()
    return {
        "id": project.id,
        "name": project.name,
        "service_identifier": project.service_identifier,
        "ingestion_key": key,
    }


@router.get("/api/projects", dependencies=[Depends(admin_auth)])
async def projects(session: SessionDep) -> list[dict[str, Any]]:
    rows = (await session.scalars(select(Project).order_by(Project.created_at))).all()
    return [
        {
            "id": p.id,
            "name": p.name,
            "service_identifier": p.service_identifier,
            "created_at": p.created_at,
        }
        for p in rows
    ]


@router.post("/v1/traces", status_code=200)
async def ingest(
    request: Request, session: SessionDep, x_agentscope_key: Annotated[str | None, Header()] = None
) -> dict[str, Any]:
    project = await project_for_key(session, x_agentscope_key)
    content_type = request.headers.get("content-type", "").split(";")[0]
    if content_type not in {"application/json", "application/x-protobuf"}:
        raise HTTPException(status_code=415, detail="supported types: JSON and x-protobuf")
    raw = await request.body()
    if len(raw) > get_settings().max_request_bytes:
        raise HTTPException(status_code=413, detail="payload too large")
    try:
        if content_type == "application/json":
            payload = await request.json()
        else:
            message = ExportTraceServiceRequest()
            message.ParseFromString(raw)
            payload = _protobuf_dict(message)
        parsed = parse(payload, get_settings().max_spans_per_request)
    except (OtlpError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    trace_ids = {s.trace_id for s in parsed}
    existing_traces = {
        t.trace_id: t
        for t in (
            await session.scalars(
                select(Trace).where(Trace.project_id == project.id, Trace.trace_id.in_(trace_ids))
            )
        ).all()
    }
    existing_spans = {
        (s.trace_id, s.span_id): s
        for s in (
            await session.scalars(
                select(Span).where(Span.project_id == project.id, Span.trace_id.in_(trace_ids))
            )
        ).all()
    }
    by_trace: dict[str, list[Any]] = {}
    for item in parsed:
        by_trace.setdefault(item.trace_id, []).append(item)
    accepted = 0
    try:
        for trace_id, items in by_trace.items():
            start, end = min(i.started_at for i in items), max(i.ended_at for i in items)
            root = next((i for i in items if not i.parent_span_id), items[0])
            trace = existing_traces.get(trace_id)
            if trace is None:
                trace = Trace(
                    project_id=project.id,
                    trace_id=trace_id,
                    root_operation=root.name,
                    service_name=root.service_name,
                    started_at=start,
                    ended_at=end,
                    duration_ms=(end - start).total_seconds() * 1000,
                    status="ERROR" if any(i.status == "ERROR" for i in items) else "OK",
                )
                session.add(trace)
                await session.flush()
                existing_traces[trace_id] = trace
            else:
                trace.started_at, trace.ended_at = (
                    min(_aware(trace.started_at), start),
                    max(_aware(trace.ended_at), end),
                )
                trace.duration_ms = (trace.ended_at - trace.started_at).total_seconds() * 1000
            for item in items:
                attrs = item.attributes
                model = attrs.get("gen_ai.request.model") or attrs.get("gen_ai.response.model")
                provider = attrs.get("gen_ai.system") or attrs.get("gen_ai.provider.name")
                input_tokens = _integer(attrs.get("gen_ai.usage.input_tokens"))
                output_tokens = _integer(attrs.get("gen_ai.usage.output_tokens"))
                cached_tokens = _integer(attrs.get("gen_ai.usage.cache_read.input_tokens"))
                cost, version = calculate(
                    str(provider) if provider else None,
                    str(model) if model else None,
                    input_tokens,
                    output_tokens,
                    cached_tokens,
                    item.started_at.date(),
                )
                values = dict(
                    parent_span_id=item.parent_span_id,
                    operation_name=item.name,
                    span_kind=item.kind,
                    service_name=item.service_name,
                    started_at=item.started_at,
                    ended_at=item.ended_at,
                    duration_ms=(item.ended_at - item.started_at).total_seconds() * 1000,
                    status=item.status,
                    error_information=item.error,
                    attributes=attrs,
                    model=model,
                    provider=provider,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    estimated_cost=cost,
                    pricing_version=version,
                )
                span = existing_spans.get((trace_id, item.span_id))
                if span:
                    for key, value in values.items():
                        setattr(span, key, value)
                else:
                    session.add(
                        Span(
                            project_id=project.id,
                            trace_pk=trace.id,
                            trace_id=trace_id,
                            span_id=item.span_id,
                            **values,
                        )
                    )
                accepted += 1
        await session.commit()
    except Exception:
        await session.rollback()
        raise HTTPException(status_code=503, detail="telemetry persistence failed") from None
    return {"partialSuccess": {}, "acceptedSpans": accepted}


def _integer(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # OTLP intValue is JSON-encoded as a string.
    except (TypeError, ValueError):
        return None


async def scoped_project(project_id: str, session: AsyncSession) -> Project:
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    return project


@router.get("/api/projects/{project_id}/traces", dependencies=[Depends(admin_auth)])
async def traces(
    project_id: str,
    session: SessionDep,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status_filter: str | None = Query(None, alias="status"),
) -> dict[str, Any]:
    await scoped_project(project_id, session)
    query = select(Trace).where(Trace.project_id == project_id)
    if status_filter:
        query = query.where(Trace.status == status_filter)
    rows = (
        await session.scalars(query.order_by(Trace.started_at.desc()).limit(limit).offset(offset))
    ).all()
    return {"items": [_trace(t) for t in rows], "limit": limit, "offset": offset}


@router.get("/api/projects/{project_id}/traces/{trace_id}", dependencies=[Depends(admin_auth)])
async def trace_detail(project_id: str, trace_id: str, session: SessionDep) -> dict[str, Any]:
    trace = await session.scalar(
        select(Trace).where(Trace.project_id == project_id, Trace.trace_id == trace_id)
    )
    if not trace:
        raise HTTPException(status_code=404, detail="trace not found")
    spans = (
        await session.scalars(
            select(Span)
            .where(Span.project_id == project_id, Span.trace_id == trace_id)
            .order_by(Span.started_at)
        )
    ).all()
    return {
        **_trace(trace),
        "spans": [_span(s) for s in spans],
        "critical_path_ms": critical_path_ms(list(spans)),
    }


def _trace(t: Trace) -> dict[str, Any]:
    return {
        "trace_id": t.trace_id,
        "root_operation": t.root_operation,
        "service_name": t.service_name,
        "started_at": t.started_at,
        "ended_at": t.ended_at,
        "duration_ms": t.duration_ms,
        "status": t.status,
    }


def _span(s: Span) -> dict[str, Any]:
    return {
        "span_id": s.span_id,
        "parent_span_id": s.parent_span_id,
        "operation_name": s.operation_name,
        "span_kind": s.span_kind,
        "started_at": s.started_at,
        "ended_at": s.ended_at,
        "duration_ms": s.duration_ms,
        "status": s.status,
        "attributes": s.attributes,
        "model": s.model,
        "provider": s.provider,
        "input_tokens": s.input_tokens,
        "output_tokens": s.output_tokens,
        "estimated_cost": str(s.estimated_cost) if s.estimated_cost is not None else None,
    }


@router.get("/api/projects/{project_id}/overview", dependencies=[Depends(admin_auth)])
async def overview(project_id: str, session: SessionDep) -> dict[str, Any]:
    await scoped_project(project_id, session)
    traces_count = await session.scalar(
        select(func.count()).select_from(Trace).where(Trace.project_id == project_id)
    )
    errors = await session.scalar(
        select(func.count())
        .select_from(Trace)
        .where(Trace.project_id == project_id, Trace.status == "ERROR")
    )
    usage = await session.execute(
        select(
            func.sum(Span.input_tokens),
            func.sum(Span.output_tokens),
            func.sum(Span.estimated_cost),
            func.avg(Trace.duration_ms),
        )
        .select_from(Span)
        .join(Trace, Span.trace_pk == Trace.id)
        .where(Span.project_id == project_id)
    )
    inp, out, cost, avg = usage.one()
    return {
        "requests": traces_count or 0,
        "errors": errors or 0,
        "error_rate": (errors or 0) / (traces_count or 1),
        "input_tokens": inp,
        "output_tokens": out,
        "estimated_cost": str(cost) if cost is not None else None,
        "average_latency_ms": avg,
    }


@router.get("/api/projects/{project_id}/usage", dependencies=[Depends(admin_auth)])
async def usage(project_id: str, session: SessionDep) -> dict[str, Any]:
    await scoped_project(project_id, session)
    rows = (
        await session.execute(
            select(
                Span.provider, Span.model, func.sum(Span.input_tokens), func.sum(Span.output_tokens)
            )
            .where(Span.project_id == project_id)
            .group_by(Span.provider, Span.model)
        )
    ).all()
    return {
        "items": [
            {"provider": p, "model": m, "input_tokens": i, "output_tokens": o}
            for p, m, i, o in rows
        ]
    }


@router.get("/api/projects/{project_id}/costs", dependencies=[Depends(admin_auth)])
async def costs(project_id: str, session: SessionDep) -> dict[str, Any]:
    await scoped_project(project_id, session)
    rows = (
        await session.execute(
            select(Span.provider, Span.model, func.sum(Span.estimated_cost), func.count())
            .where(Span.project_id == project_id)
            .group_by(Span.provider, Span.model)
        )
    ).all()
    return {
        "items": [
            {
                "provider": p,
                "model": m,
                "estimated_cost": str(c) if c is not None else None,
                "span_count": n,
                "pricing_status": "known" if c is not None else "unknown",
            }
            for p, m, c, n in rows
        ]
    }


@router.get("/api/projects/{project_id}/models", dependencies=[Depends(admin_auth)])
async def models(project_id: str, session: SessionDep) -> list[dict[str, Any]]:
    await scoped_project(project_id, session)
    rows = (
        await session.execute(
            select(Span.provider, Span.model)
            .where(Span.project_id == project_id, Span.model.is_not(None))
            .distinct()
        )
    ).all()
    return [{"provider": p, "model": m} for p, m in rows]


@router.post("/api/projects/{project_id}/diagnostics/run", dependencies=[Depends(admin_auth)])
async def run_diagnostics(project_id: str, session: SessionDep) -> dict[str, int]:
    await scoped_project(project_id, session)
    traces = (await session.scalars(select(Trace).where(Trace.project_id == project_id))).all()
    created = 0
    for trace in traces:
        spans = (
            await session.scalars(
                select(Span).where(Span.project_id == project_id, Span.trace_id == trace.trace_id)
            )
        ).all()
        findings = repeated_tool_calls(list(spans))
        for span in spans:
            if (
                span.input_tokens is not None or span.output_tokens is not None
            ) and span.estimated_cost is None:
                findings.append(
                    {
                        "rule_id": "AS005",
                        "severity": "info",
                        "title": "Unknown model pricing",
                        "description": "Usage was reported but no matching versioned price exists.",
                        "evidence": {
                            "provider": span.provider,
                            "model": span.model,
                            "classification": "direct",
                        },
                        "recommendation": (
                            "Configure a versioned custom price for this provider and model."
                        ),
                        "span_id": span.span_id,
                    }
                )
        for f in findings:
            exists = await session.scalar(
                select(DiagnosticFinding.id).where(
                    DiagnosticFinding.project_id == project_id,
                    DiagnosticFinding.trace_id == trace.trace_id,
                    DiagnosticFinding.rule_id == f["rule_id"],
                    DiagnosticFinding.span_id == f["span_id"],
                )
            )
            if not exists:
                session.add(DiagnosticFinding(project_id=project_id, trace_id=trace.trace_id, **f))
                created += 1
    await session.commit()
    return {"created": created}


@router.get("/api/projects/{project_id}/diagnostics", dependencies=[Depends(admin_auth)])
async def diagnostics(project_id: str, session: SessionDep) -> list[dict[str, Any]]:
    await scoped_project(project_id, session)
    rows = (
        await session.scalars(
            select(DiagnosticFinding)
            .where(DiagnosticFinding.project_id == project_id)
            .order_by(DiagnosticFinding.created_at.desc())
        )
    ).all()
    return [
        {
            "id": f.id,
            "trace_id": f.trace_id,
            "span_id": f.span_id,
            "rule_id": f.rule_id,
            "severity": f.severity,
            "title": f.title,
            "description": f.description,
            "evidence": f.evidence,
            "recommendation": f.recommendation,
            "created_at": f.created_at,
        }
        for f in rows
    ]
