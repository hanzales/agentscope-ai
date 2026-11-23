import asyncio
import hashlib
import hmac
import ipaddress
import json
import socket
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any
from urllib.parse import urlparse

import httpx
from app.analytics.pricing import calculate, calculate_rates
from app.core.config import get_settings
from app.core.security import hash_api_key, issue_access_token, issue_api_key, verify_api_key
from app.diagnostics.engine import (
    agent_bottleneck,
    critical_path_ms,
    high_retry_count,
    repeated_tool_calls,
    token_usage_spike,
)
from app.ingestion.otlp import OtlpError, parse
from app.models import (
    AlertEvent,
    AlertRule,
    Budget,
    DiagnosticFinding,
    ModelPricing,
    Project,
    ProjectAccessToken,
    ProjectMember,
    Span,
    Trace,
    WebhookEndpoint,
)
from app.storage.database import get_session
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()
SessionDep = Annotated[AsyncSession, Depends(get_session)]


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    service_identifier: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=120)


class PricingCreate(BaseModel):
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=255)
    effective_date: date
    input_per_million: Decimal = Field(ge=0)
    output_per_million: Decimal = Field(ge=0)
    cached_per_million: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    source: str = Field(default="custom", min_length=1, max_length=255)
    version: str = Field(min_length=1, max_length=64)


class ProjectSettings(BaseModel):
    retention_days: int = Field(ge=1, le=3650)
    sample_rate: float = Field(ge=0.01, le=1.0)


class BudgetUpsert(BaseModel):
    limit_amount: Decimal = Field(gt=0)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    enabled: bool = True


class AlertRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    metric: str = Field(pattern=r"^(error_rate|p95_latency_ms|estimated_cost)$")
    threshold: Decimal = Field(gt=0)
    enabled: bool = True


class AccessTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(pattern=r"^(viewer|editor|admin)$")


class MemberUpsert(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=320)
    role: str = Field(pattern=r"^(viewer|editor|admin)$")


class WebhookCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=8, max_length=2048)
    enabled: bool = True


async def _validate_webhook_url(value: str) -> None:
    parsed = urlparse(value)
    settings = get_settings()
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username:
        raise HTTPException(status_code=422, detail="webhook URL must be an absolute HTTP(S) URL")
    if parsed.scheme != "https" and not (
        settings.demo_mode and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise HTTPException(status_code=422, detail="webhook URL must use HTTPS")
    try:
        records = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, parsed.port or 443)
    except OSError as exc:
        raise HTTPException(status_code=422, detail="webhook host cannot be resolved") from exc
    if not settings.webhook_allow_private_urls:
        for record in records:
            address = ipaddress.ip_address(record[4][0])
            if address.is_private or address.is_loopback or address.is_link_local:
                if not (
                    settings.demo_mode and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                ):
                    raise HTTPException(
                        status_code=422, detail="private webhook targets are disabled"
                    )


async def _deliver_webhooks(endpoints: list[str], payload: dict[str, Any]) -> None:
    body = json.dumps(payload, separators=(",", ":"), default=str).encode()
    settings = get_settings()
    secret = settings.webhook_signing_secret or ("demo-only" if settings.demo_mode else "")
    if not secret:
        return
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
        for endpoint in endpoints:
            try:
                await _validate_webhook_url(endpoint)
                response = await client.post(
                    endpoint,
                    content=body,
                    headers={
                        "content-type": "application/json",
                        "x-tamga-signature-256": f"sha256={signature}",
                    },
                )
                response.raise_for_status()
            except Exception:
                # Delivery is isolated from requests; operators can retry evaluation.
                continue


async def admin_auth(
    request: Request,
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    settings = get_settings()
    if settings.demo_mode:
        request.state.access_role = "admin"
        return
    expected = settings.admin_token
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if expected and secrets_compare(supplied, expected):
        request.state.access_role = "admin"
        return
    project_id = request.path_params.get("project_id")
    if not supplied:
        raise HTTPException(status_code=401, detail="invalid credentials")
    if not project_id and request.method == "GET" and request.url.path == "/api/projects":
        token = await session.scalar(
            select(ProjectAccessToken).where(
                ProjectAccessToken.token_hash == hash_api_key(supplied)
            )
        )
        if token is None or not secrets_compare(hash_api_key(supplied), token.token_hash):
            raise HTTPException(status_code=401, detail="invalid credentials")
        request.state.access_role = token.role
        request.state.allowed_project_id = token.project_id
        token.last_used_at = datetime.now(UTC)
        await session.commit()
        return
    if not project_id:
        raise HTTPException(status_code=401, detail="admin credentials required")
    token = await session.scalar(
        select(ProjectAccessToken).where(
            ProjectAccessToken.project_id == project_id,
            ProjectAccessToken.token_hash == hash_api_key(supplied),
        )
    )
    if token is None or not secrets_compare(hash_api_key(supplied), token.token_hash):
        raise HTTPException(status_code=401, detail="invalid credentials")
    if request.method not in {"GET", "HEAD"} and token.role == "viewer":
        raise HTTPException(status_code=403, detail="viewer role is read-only")
    token.last_used_at = datetime.now(UTC)
    await session.commit()
    request.state.access_role = token.role


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
async def projects(request: Request, session: SessionDep) -> list[dict[str, Any]]:
    query = select(Project).order_by(Project.created_at)
    allowed = getattr(request.state, "allowed_project_id", None)
    if allowed:
        query = query.where(Project.id == allowed)
    rows = (await session.scalars(query)).all()
    return [
        {
            "id": p.id,
            "name": p.name,
            "service_identifier": p.service_identifier,
            "created_at": p.created_at,
            "retention_days": p.retention_days,
            "sample_rate": p.sample_rate,
        }
        for p in rows
    ]


@router.post(
    "/api/projects/{project_id}/keys/rotate",
    dependencies=[Depends(admin_auth)],
)
async def rotate_ingestion_key(project_id: str, session: SessionDep) -> dict[str, str]:
    project = await scoped_project(project_id, session)
    key = issue_api_key()
    project.api_key_hash = hash_api_key(key)
    await session.commit()
    return {
        "project_id": project.id,
        "ingestion_key": key,
        "message": "Previous ingestion key revoked; store this key now.",
    }


def require_project_admin(request: Request) -> None:
    if getattr(request.state, "access_role", None) != "admin":
        raise HTTPException(status_code=403, detail="project admin role required")


@router.get("/api/projects/{project_id}/access", dependencies=[Depends(admin_auth)])
async def project_access(project_id: str, request: Request, session: SessionDep) -> dict[str, Any]:
    require_project_admin(request)
    await scoped_project(project_id, session)
    members = (
        await session.scalars(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .order_by(ProjectMember.email)
        )
    ).all()
    tokens = (
        await session.scalars(
            select(ProjectAccessToken)
            .where(ProjectAccessToken.project_id == project_id)
            .order_by(ProjectAccessToken.created_at)
        )
    ).all()
    return {
        "members": [
            {"id": row.id, "email": row.email, "role": row.role, "created_at": row.created_at}
            for row in members
        ],
        "tokens": [
            {
                "id": row.id,
                "name": row.name,
                "role": row.role,
                "created_at": row.created_at,
                "last_used_at": row.last_used_at,
            }
            for row in tokens
        ],
    }


@router.put("/api/projects/{project_id}/access/members", dependencies=[Depends(admin_auth)])
async def upsert_member(
    project_id: str, data: MemberUpsert, request: Request, session: SessionDep
) -> dict[str, Any]:
    require_project_admin(request)
    await scoped_project(project_id, session)
    normalized = data.email.lower()
    member = await session.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id, ProjectMember.email == normalized
        )
    )
    if member is None:
        member = ProjectMember(project_id=project_id, email=normalized, role=data.role)
        session.add(member)
    else:
        member.role = data.role
    await session.commit()
    await session.refresh(member)
    return {"id": member.id, "email": member.email, "role": member.role}


@router.post(
    "/api/projects/{project_id}/access/tokens",
    status_code=201,
    dependencies=[Depends(admin_auth)],
)
async def create_access_token(
    project_id: str, data: AccessTokenCreate, request: Request, session: SessionDep
) -> dict[str, Any]:
    require_project_admin(request)
    await scoped_project(project_id, session)
    raw = issue_access_token()
    token = ProjectAccessToken(
        project_id=project_id, name=data.name, role=data.role, token_hash=hash_api_key(raw)
    )
    session.add(token)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise HTTPException(status_code=409, detail="access token name already exists") from None
    return {
        "id": token.id,
        "name": token.name,
        "role": token.role,
        "access_token": raw,
        "message": "This project access token is shown once; store it securely.",
    }


@router.delete(
    "/api/projects/{project_id}/access/tokens/{token_id}",
    status_code=204,
    dependencies=[Depends(admin_auth)],
)
async def revoke_access_token(
    project_id: str, token_id: int, request: Request, session: SessionDep
) -> None:
    require_project_admin(request)
    token = await session.scalar(
        select(ProjectAccessToken).where(
            ProjectAccessToken.id == token_id, ProjectAccessToken.project_id == project_id
        )
    )
    if token is None:
        raise HTTPException(status_code=404, detail="access token not found")
    await session.delete(token)
    await session.commit()


@router.patch(
    "/api/projects/{project_id}/settings",
    dependencies=[Depends(admin_auth)],
)
async def update_project_settings(
    project_id: str, data: ProjectSettings, session: SessionDep
) -> dict[str, Any]:
    project = await scoped_project(project_id, session)
    project.retention_days = data.retention_days
    project.sample_rate = data.sample_rate
    await session.commit()
    return {
        "project_id": project.id,
        "retention_days": project.retention_days,
        "sample_rate": project.sample_rate,
    }


@router.post(
    "/api/projects/{project_id}/retention/run",
    dependencies=[Depends(admin_auth)],
)
async def run_retention(
    project_id: str, session: SessionDep, dry_run: bool = Query(True)
) -> dict[str, Any]:
    project = await scoped_project(project_id, session)
    cutoff = datetime.now(UTC) - timedelta(days=project.retention_days)
    traces = (
        await session.scalars(
            select(Trace).where(Trace.project_id == project_id, Trace.started_at < cutoff)
        )
    ).all()
    if not dry_run:
        for trace in traces:
            await session.delete(trace)
        await session.commit()
    return {
        "dry_run": dry_run,
        "retention_days": project.retention_days,
        "cutoff": cutoff,
        "matched_traces": len(traces),
        "deleted_traces": 0 if dry_run else len(traces),
    }


@router.get(
    "/api/projects/{project_id}/pricing",
    dependencies=[Depends(admin_auth)],
)
async def list_pricing(project_id: str, session: SessionDep) -> list[dict[str, Any]]:
    await scoped_project(project_id, session)
    rows = (
        await session.scalars(
            select(ModelPricing)
            .where(ModelPricing.project_id == project_id)
            .order_by(ModelPricing.provider, ModelPricing.model, ModelPricing.effective_date.desc())
        )
    ).all()
    return [_pricing(row) for row in rows]


@router.post(
    "/api/projects/{project_id}/pricing",
    status_code=201,
    dependencies=[Depends(admin_auth)],
)
async def create_pricing(
    project_id: str, data: PricingCreate, session: SessionDep
) -> dict[str, Any]:
    await scoped_project(project_id, session)
    effective = datetime.combine(data.effective_date, time.min, tzinfo=UTC)
    exists = await session.scalar(
        select(ModelPricing.id).where(
            ModelPricing.project_id == project_id,
            ModelPricing.provider == data.provider,
            ModelPricing.model == data.model,
            ModelPricing.effective_date == effective,
        )
    )
    if exists:
        raise HTTPException(status_code=409, detail="pricing version already exists")
    row = ModelPricing(
        project_id=project_id,
        provider=data.provider,
        model=data.model,
        effective_date=effective,
        input_per_million=data.input_per_million,
        output_per_million=data.output_per_million,
        cached_per_million=data.cached_per_million,
        currency=data.currency,
        source=data.source,
        version=data.version,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _pricing(row)


@router.delete(
    "/api/projects/{project_id}/pricing/{pricing_id}",
    status_code=204,
    dependencies=[Depends(admin_auth)],
)
async def delete_pricing(project_id: str, pricing_id: int, session: SessionDep) -> None:
    row = await session.scalar(
        select(ModelPricing).where(
            ModelPricing.id == pricing_id, ModelPricing.project_id == project_id
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="pricing version not found")
    await session.delete(row)
    await session.commit()


def _pricing(row: ModelPricing) -> dict[str, Any]:
    return {
        "id": row.id,
        "provider": row.provider,
        "model": row.model,
        "effective_date": row.effective_date,
        "input_per_million": str(row.input_per_million),
        "output_per_million": str(row.output_per_million),
        "cached_per_million": (
            str(row.cached_per_million) if row.cached_per_million is not None else None
        ),
        "currency": row.currency,
        "source": row.source,
        "version": row.version,
    }


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
    sample_rate = project.sample_rate if project.sample_rate is not None else 1.0
    error_trace_ids = {span.trace_id for span in parsed if span.status == "ERROR"}
    kept_trace_ids = {
        span.trace_id
        for span in parsed
        if span.trace_id in error_trace_ids or int(span.trace_id, 16) / float(2**128) < sample_rate
    }
    sampled_out = sum(1 for span in parsed if span.trace_id not in kept_trace_ids)
    parsed = [span for span in parsed if span.trace_id in kept_trace_ids]
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
            deployment_environment = root.attributes.get("deployment.environment.name")
            service_version = root.attributes.get("service.version")
            git_commit = root.attributes.get("vcs.ref.head.revision") or root.attributes.get(
                "git.commit.sha"
            )
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
                    deployment_environment=(
                        str(deployment_environment) if deployment_environment else None
                    ),
                    service_version=str(service_version) if service_version else None,
                    git_commit=str(git_commit)[:64] if git_commit else None,
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
                trace.deployment_environment = (
                    str(deployment_environment)
                    if deployment_environment
                    else trace.deployment_environment
                )
                trace.service_version = (
                    str(service_version) if service_version else trace.service_version
                )
                trace.git_commit = str(git_commit)[:64] if git_commit else trace.git_commit
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
                if provider and model:
                    custom_price = await session.scalar(
                        select(ModelPricing)
                        .where(
                            ModelPricing.project_id == project.id,
                            ModelPricing.provider == str(provider),
                            ModelPricing.model == str(model),
                            ModelPricing.effective_date <= item.started_at,
                        )
                        .order_by(ModelPricing.effective_date.desc())
                        .limit(1)
                    )
                    if custom_price is not None:
                        cost = calculate_rates(
                            input_tokens,
                            output_tokens,
                            cached_tokens,
                            custom_price.input_per_million,
                            custom_price.output_per_million,
                            custom_price.cached_per_million,
                        )
                        version = custom_price.version
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
    partial_success = (
        {
            "rejectedSpans": sampled_out,
            "errorMessage": "deterministically sampled by project policy",
        }
        if sampled_out
        else {}
    )
    return {"partialSuccess": partial_success, "acceptedSpans": accepted}


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
    search: str | None = Query(None, max_length=255),
    service: str | None = Query(None, max_length=255),
    agent: str | None = Query(None, max_length=255),
    model: str | None = Query(None, max_length=255),
    min_duration_ms: float | None = Query(None, ge=0),
    started_after: datetime | None = None,
    started_before: datetime | None = None,
) -> dict[str, Any]:
    await scoped_project(project_id, session)
    query = select(Trace).where(Trace.project_id == project_id)
    if status_filter:
        query = query.where(Trace.status == status_filter)
    if search:
        query = query.where(
            or_(Trace.trace_id.ilike(f"%{search}%"), Trace.root_operation.ilike(f"%{search}%"))
        )
    if service:
        query = query.where(Trace.service_name == service)
    if agent:
        query = query.where(Trace.root_operation == agent)
    if model:
        query = query.where(Trace.spans.any(Span.model == model))
    if min_duration_ms is not None:
        query = query.where(Trace.duration_ms >= min_duration_ms)
    if started_after:
        query = query.where(Trace.started_at >= started_after)
    if started_before:
        query = query.where(Trace.started_at <= started_before)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        await session.scalars(query.order_by(Trace.started_at.desc()).limit(limit).offset(offset))
    ).all()
    return {
        "items": [_trace(t) for t in rows],
        "limit": limit,
        "offset": offset,
        "total": total or 0,
    }


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
        "deployment_environment": t.deployment_environment,
        "service_version": t.service_version,
        "git_commit": t.git_commit,
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
    durations = list(
        await session.scalars(select(Trace.duration_ms).where(Trace.project_id == project_id))
    )
    return {
        "requests": traces_count or 0,
        "errors": errors or 0,
        "error_rate": (errors or 0) / (traces_count or 1),
        "input_tokens": inp,
        "output_tokens": out,
        "estimated_cost": str(cost) if cost is not None else None,
        "average_latency_ms": avg,
        "p50_latency_ms": _percentile(durations, 0.50),
        "p95_latency_ms": _percentile(durations, 0.95),
    }


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


@router.get("/api/projects/{project_id}/timeseries", dependencies=[Depends(admin_auth)])
async def timeseries(
    project_id: str,
    session: SessionDep,
    days: int = Query(7, ge=1, le=365),
) -> dict[str, Any]:
    await scoped_project(project_id, session)
    start = datetime.now(UTC) - timedelta(days=days)
    traces = (
        await session.scalars(
            select(Trace)
            .where(Trace.project_id == project_id, Trace.started_at >= start)
            .order_by(Trace.started_at)
        )
    ).all()
    spans = (
        await session.scalars(
            select(Span).where(Span.project_id == project_id, Span.started_at >= start)
        )
    ).all()
    buckets: dict[str, dict[str, Any]] = {}
    for trace in traces:
        key = _aware(trace.started_at).date().isoformat()
        bucket = buckets.setdefault(
            key,
            {
                "date": key,
                "requests": 0,
                "errors": 0,
                "tokens": 0,
                "cost": Decimal(0),
                "latencies": [],
            },
        )
        bucket["requests"] += 1
        bucket["errors"] += int(trace.status == "ERROR")
        bucket["latencies"].append(trace.duration_ms)
    for span in spans:
        key = _aware(span.started_at).date().isoformat()
        bucket = buckets.setdefault(
            key,
            {
                "date": key,
                "requests": 0,
                "errors": 0,
                "tokens": 0,
                "cost": Decimal(0),
                "latencies": [],
            },
        )
        bucket["tokens"] += (span.input_tokens or 0) + (span.output_tokens or 0)
        bucket["cost"] += span.estimated_cost or Decimal(0)
    items = []
    for key in sorted(buckets):
        bucket = buckets[key]
        requests = bucket["requests"]
        items.append(
            {
                "date": key,
                "requests": requests,
                "errors": bucket["errors"],
                "error_rate": bucket["errors"] / requests if requests else 0,
                "tokens": bucket["tokens"],
                "estimated_cost": str(bucket["cost"]),
                "p50_latency_ms": _percentile(bucket["latencies"], 0.5),
                "p95_latency_ms": _percentile(bucket["latencies"], 0.95),
            }
        )
    return {"days": days, "items": items}


@router.get("/api/projects/{project_id}/performance", dependencies=[Depends(admin_auth)])
async def performance(project_id: str, session: SessionDep) -> dict[str, Any]:
    await scoped_project(project_id, session)
    traces = (await session.scalars(select(Trace).where(Trace.project_id == project_id))).all()
    spans = (await session.scalars(select(Span).where(Span.project_id == project_id))).all()
    agent_groups: dict[str, list[Trace]] = {}
    for trace in traces:
        agent_groups.setdefault(trace.root_operation, []).append(trace)
    tool_groups: dict[str, list[Span]] = {}
    for span in spans:
        tool = span.attributes.get("tool.name") or span.attributes.get("gen_ai.tool.name")
        if tool:
            tool_groups.setdefault(str(tool), []).append(span)
    return {
        "agents": [
            {
                "name": name,
                "requests": len(items),
                "error_rate": sum(item.status == "ERROR" for item in items) / len(items),
                "average_latency_ms": sum(item.duration_ms for item in items) / len(items),
                "p95_latency_ms": _percentile([item.duration_ms for item in items], 0.95),
            }
            for name, items in sorted(agent_groups.items())
        ],
        "tools": [
            {
                "name": name,
                "calls": len(items),
                "error_rate": sum(item.status == "ERROR" for item in items) / len(items),
                "average_latency_ms": sum(item.duration_ms for item in items) / len(items),
                "p95_latency_ms": _percentile([item.duration_ms for item in items], 0.95),
            }
            for name, items in sorted(tool_groups.items())
        ],
    }


def _release_key(trace: Trace) -> str:
    return trace.service_version or trace.git_commit or "unversioned"


def _release_summary(name: str, rows: list[Trace]) -> dict[str, Any]:
    durations = [row.duration_ms for row in rows]
    return {
        "release": name,
        "requests": len(rows),
        "errors": sum(row.status == "ERROR" for row in rows),
        "error_rate": sum(row.status == "ERROR" for row in rows) / len(rows),
        "p50_latency_ms": _percentile(durations, 0.5),
        "p95_latency_ms": _percentile(durations, 0.95),
        "first_seen": min(_aware(row.started_at) for row in rows),
        "last_seen": max(_aware(row.started_at) for row in rows),
        "environments": sorted({row.deployment_environment or "unknown" for row in rows}),
    }


@router.get("/api/projects/{project_id}/deployments", dependencies=[Depends(admin_auth)])
async def deployments(project_id: str, session: SessionDep) -> dict[str, Any]:
    await scoped_project(project_id, session)
    rows = (
        await session.scalars(
            select(Trace).where(Trace.project_id == project_id).order_by(Trace.started_at.desc())
        )
    ).all()
    groups: dict[str, list[Trace]] = {}
    for trace in rows:
        groups.setdefault(_release_key(trace), []).append(trace)
    return {"items": [_release_summary(name, items) for name, items in groups.items()]}


@router.get("/api/projects/{project_id}/deployments/compare", dependencies=[Depends(admin_auth)])
async def compare_deployments(
    project_id: str,
    baseline: str,
    candidate: str,
    session: SessionDep,
) -> dict[str, Any]:
    await scoped_project(project_id, session)
    rows = (await session.scalars(select(Trace).where(Trace.project_id == project_id))).all()
    groups = {
        name: [row for row in rows if _release_key(row) == name] for name in (baseline, candidate)
    }
    if not groups[baseline] or not groups[candidate]:
        raise HTTPException(status_code=404, detail="baseline or candidate release not found")
    left = _release_summary(baseline, groups[baseline])
    right = _release_summary(candidate, groups[candidate])
    return {
        "baseline": left,
        "candidate": right,
        "delta": {
            "error_rate": right["error_rate"] - left["error_rate"],
            "p50_latency_ms": right["p50_latency_ms"] - left["p50_latency_ms"],
            "p95_latency_ms": right["p95_latency_ms"] - left["p95_latency_ms"],
        },
    }


@router.get("/api/projects/{project_id}/live/traces", dependencies=[Depends(admin_auth)])
async def trace_stream(project_id: str, request: Request, session: SessionDep) -> StreamingResponse:
    await scoped_project(project_id, session)

    async def events():
        last_id = int(
            await session.scalar(select(func.max(Trace.id)).where(Trace.project_id == project_id))
            or 0
        )
        yield "event: ready\ndata: {}\n\n"
        while not await request.is_disconnected():
            rows = (
                await session.scalars(
                    select(Trace)
                    .where(Trace.project_id == project_id, Trace.id > last_id)
                    .order_by(Trace.id)
                    .limit(100)
                )
            ).all()
            for trace in rows:
                last_id = max(last_id, trace.id)
                yield f"event: trace\ndata: {json.dumps(_trace(trace), default=str)}\n\n"
            if not rows:
                yield ": keepalive\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(events(), media_type="text/event-stream")


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
            select(
                Span.provider,
                Span.model,
                func.sum(Span.estimated_cost),
                func.count(),
                func.sum(
                    case(
                        (
                            and_(
                                or_(
                                    Span.input_tokens.is_not(None), Span.output_tokens.is_not(None)
                                ),
                                Span.estimated_cost.is_(None),
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
            )
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
                "unknown_usage_spans": unknown_count,
                "pricing_status": (
                    "partial"
                    if c is not None and unknown_count
                    else "known"
                    if c is not None
                    else "unknown"
                ),
            }
            for p, m, c, n, unknown_count in rows
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


@router.get("/api/projects/{project_id}/budget", dependencies=[Depends(admin_auth)])
async def get_budget(project_id: str, session: SessionDep) -> dict[str, Any]:
    await scoped_project(project_id, session)
    budget = await session.scalar(
        select(Budget).where(Budget.project_id == project_id, Budget.period == "monthly")
    )
    month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    spent = await session.scalar(
        select(func.sum(Span.estimated_cost)).where(
            Span.project_id == project_id, Span.started_at >= month_start
        )
    )
    return {
        "period": "monthly",
        "limit_amount": str(budget.limit_amount) if budget else None,
        "currency": budget.currency if budget else "USD",
        "enabled": budget.enabled if budget else False,
        "spent_amount": str(spent or Decimal(0)),
        "usage_percent": (
            float((spent or Decimal(0)) / budget.limit_amount * 100) if budget else None
        ),
    }


@router.put("/api/projects/{project_id}/budget", dependencies=[Depends(admin_auth)])
async def put_budget(project_id: str, data: BudgetUpsert, session: SessionDep) -> dict[str, Any]:
    await scoped_project(project_id, session)
    budget = await session.scalar(
        select(Budget).where(Budget.project_id == project_id, Budget.period == "monthly")
    )
    if budget is None:
        budget = Budget(project_id=project_id, period="monthly", limit_amount=data.limit_amount)
        session.add(budget)
    budget.limit_amount = data.limit_amount
    budget.currency = data.currency
    budget.enabled = data.enabled
    await session.commit()
    return await get_budget(project_id, session)


@router.get("/api/projects/{project_id}/alerts", dependencies=[Depends(admin_auth)])
async def alerts(project_id: str, session: SessionDep) -> dict[str, Any]:
    await scoped_project(project_id, session)
    rules = (
        await session.scalars(
            select(AlertRule)
            .where(AlertRule.project_id == project_id)
            .order_by(AlertRule.created_at)
        )
    ).all()
    events = (
        await session.scalars(
            select(AlertEvent)
            .where(AlertEvent.project_id == project_id)
            .order_by(AlertEvent.created_at.desc())
            .limit(100)
        )
    ).all()
    return {
        "rules": [
            {
                "id": rule.id,
                "name": rule.name,
                "metric": rule.metric,
                "threshold": str(rule.threshold),
                "enabled": rule.enabled,
            }
            for rule in rules
        ],
        "events": [
            {
                "id": event.id,
                "rule_id": event.rule_id,
                "severity": event.severity,
                "title": event.title,
                "metric": event.metric,
                "observed_value": str(event.observed_value),
                "threshold": str(event.threshold),
                "created_at": event.created_at,
            }
            for event in events
        ],
    }


@router.get("/api/projects/{project_id}/webhooks", dependencies=[Depends(admin_auth)])
async def list_webhooks(project_id: str, session: SessionDep) -> list[dict[str, Any]]:
    await scoped_project(project_id, session)
    rows = (
        await session.scalars(
            select(WebhookEndpoint)
            .where(WebhookEndpoint.project_id == project_id)
            .order_by(WebhookEndpoint.created_at)
        )
    ).all()
    return [
        {"id": row.id, "name": row.name, "url": row.url, "enabled": row.enabled} for row in rows
    ]


@router.post(
    "/api/projects/{project_id}/webhooks",
    status_code=201,
    dependencies=[Depends(admin_auth)],
)
async def create_webhook(
    project_id: str, data: WebhookCreate, request: Request, session: SessionDep
) -> dict[str, Any]:
    require_project_admin(request)
    await scoped_project(project_id, session)
    if not get_settings().demo_mode and not get_settings().webhook_signing_secret:
        raise HTTPException(status_code=503, detail="webhook signing secret is not configured")
    await _validate_webhook_url(data.url)
    row = WebhookEndpoint(project_id=project_id, **data.model_dump())
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {"id": row.id, "name": row.name, "url": row.url, "enabled": row.enabled}


@router.delete(
    "/api/projects/{project_id}/webhooks/{webhook_id}",
    status_code=204,
    dependencies=[Depends(admin_auth)],
)
async def delete_webhook(
    project_id: str, webhook_id: int, request: Request, session: SessionDep
) -> None:
    require_project_admin(request)
    row = await session.scalar(
        select(WebhookEndpoint).where(
            WebhookEndpoint.id == webhook_id, WebhookEndpoint.project_id == project_id
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="webhook not found")
    await session.delete(row)
    await session.commit()


@router.post(
    "/api/projects/{project_id}/alerts/rules",
    status_code=201,
    dependencies=[Depends(admin_auth)],
)
async def create_alert_rule(
    project_id: str, data: AlertRuleCreate, session: SessionDep
) -> dict[str, Any]:
    await scoped_project(project_id, session)
    rule = AlertRule(project_id=project_id, **data.model_dump())
    session.add(rule)
    await session.commit()
    await session.refresh(rule)
    return {"id": rule.id, **data.model_dump(mode="json")}


@router.post(
    "/api/projects/{project_id}/alerts/evaluate",
    dependencies=[Depends(admin_auth)],
)
async def evaluate_alerts(
    project_id: str, background_tasks: BackgroundTasks, session: SessionDep
) -> dict[str, int]:
    await scoped_project(project_id, session)
    rules = (
        await session.scalars(
            select(AlertRule).where(AlertRule.project_id == project_id, AlertRule.enabled.is_(True))
        )
    ).all()
    traces = (await session.scalars(select(Trace).where(Trace.project_id == project_id))).all()
    costs_total = await session.scalar(
        select(func.sum(Span.estimated_cost)).where(Span.project_id == project_id)
    )
    metrics = {
        "error_rate": Decimal(str(sum(t.status == "ERROR" for t in traces) / len(traces)))
        if traces
        else Decimal(0),
        "p95_latency_ms": Decimal(str(_percentile([t.duration_ms for t in traces], 0.95) or 0)),
        "estimated_cost": costs_total or Decimal(0),
    }
    created = 0
    notifications: list[dict[str, Any]] = []
    cooldown = datetime.now(UTC) - timedelta(hours=1)
    for rule in rules:
        observed = metrics[rule.metric]
        if observed < rule.threshold:
            continue
        recent = await session.scalar(
            select(AlertEvent.id).where(
                AlertEvent.project_id == project_id,
                AlertEvent.rule_id == rule.id,
                AlertEvent.created_at >= cooldown,
            )
        )
        if recent:
            continue
        event = AlertEvent(
            project_id=project_id,
            rule_id=rule.id,
            severity="warning",
            title=f"{rule.name}: threshold exceeded",
            metric=rule.metric,
            observed_value=observed,
            threshold=rule.threshold,
        )
        session.add(event)
        notifications.append(
            {
                "type": "tamga.alert.triggered",
                "project_id": project_id,
                "rule_id": rule.id,
                "title": event.title,
                "metric": rule.metric,
                "observed_value": str(observed),
                "threshold": str(rule.threshold),
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        created += 1
    await session.commit()
    if notifications:
        endpoints = list(
            await session.scalars(
                select(WebhookEndpoint.url).where(
                    WebhookEndpoint.project_id == project_id,
                    WebhookEndpoint.enabled.is_(True),
                )
            )
        )
        for notification in notifications:
            background_tasks.add_task(_deliver_webhooks, endpoints, notification)
    return {"created": created}


@router.post("/api/projects/{project_id}/diagnostics/run", dependencies=[Depends(admin_auth)])
async def run_diagnostics(project_id: str, session: SessionDep) -> dict[str, int]:
    await scoped_project(project_id, session)
    traces = (
        await session.scalars(
            select(Trace).where(Trace.project_id == project_id).order_by(Trace.started_at)
        )
    ).all()
    all_spans = (await session.scalars(select(Span).where(Span.project_id == project_id))).all()
    spans_by_trace: dict[str, list[Span]] = {}
    for span in all_spans:
        spans_by_trace.setdefault(span.trace_id, []).append(span)
    historical_totals: list[int] = []
    created = 0
    for trace in traces:
        spans = spans_by_trace.get(trace.trace_id, [])
        findings = [
            *repeated_tool_calls(spans),
            *agent_bottleneck(spans),
            *token_usage_spike(trace.trace_id, spans, historical_totals),
            *high_retry_count(spans),
        ]
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
        trace_tokens = sum((span.input_tokens or 0) + (span.output_tokens or 0) for span in spans)
        if trace_tokens > 0:
            historical_totals.append(trace_tokens)
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
