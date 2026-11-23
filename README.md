# AgentScope AI

**Understand every agent. Explain every cost.**

AgentScope AI is a self-hosted, OpenTelemetry-native observability platform focused on one question: *why is this agent slow, failing, repetitive, or expensive?* It stores real OTLP traces, keeps unknown usage and pricing explicit, and produces deterministic evidence rather than sending telemetry to another LLM.

> Status: `0.1.0rc1`, an experimental preview. OTLP JSON/binary traces, the core Python SDK, PostgreSQL/SQLite persistence, versioned custom pricing, AS001–AS005 diagnostics, project/key management, and trace waterfall views are implemented. OpenAI/LangGraph adapters, retention jobs, production authentication UI, and rate limiting are planned.

## Quick start

Python 3.12+ and Node 22+:

```bash
python -m venv .venv
.venv/bin/pip install -e 'backend[dev]' -e 'sdk/python[dev]'
cd frontend && npm install && cd ..
cd backend && AGENTSCOPE_DATABASE_URL=sqlite+aiosqlite:///../agentscope.db ../.venv/bin/alembic upgrade head && cd ..
AGENTSCOPE_DATABASE_URL=sqlite+aiosqlite:///./agentscope.db .venv/bin/uvicorn app.main:app --app-dir backend
```

Create a project (demo mode permits this without admin authentication):

```bash
curl -s http://localhost:8000/api/projects -H 'content-type: application/json' \
  -d '{"name":"Research agent","service_identifier":"research-agent"}'
```

The response shows the ingestion key exactly once. Never put it in `NEXT_PUBLIC_*` configuration.

## SDK integration

```python
from agentscope_ai import AgentScope

scope = AgentScope(service_name="research-agent", endpoint="http://localhost:8000", api_key="asai_...")

@scope.trace_agent("research")
def research(question: str):
    with scope.model_call("demo", "simulated-model") as span:
        scope.record_usage(span, input_tokens=10, output_tokens=20, source="provider")
    return {"answer": "Example"}
```

Decorators do not infer tokens. Record only provider-reported or otherwise identified counts. Prompts, completions, arguments, and return values are not captured by decorators.

## OTLP ingestion

Send standard OTLP protobuf JSON mapping or binary protobuf to `POST /v1/traces` with `X-AgentScope-Key`. Resource/scope spans, typed attributes, parent IDs, GenAI usage fields, out-of-order trace updates, and retry-safe span identities are supported. Binary `application/x-protobuf` uses the official OpenTelemetry protocol definitions. Compatibility targets stable OTLP traces and recognizes current `gen_ai.*` attributes without discarding unknown fields.

## Docker

```bash
cp .env.example .env
# Replace POSTGRES_PASSWORD; for secured mode also set AGENTSCOPE_DEMO_MODE=false and AGENTSCOPE_ADMIN_TOKEN.
docker compose up --build -d
```

Services bind to loopback by default. PostgreSQL has a persistent volume and service health checks. Docker is not required for SDK development.

## Security and privacy

Ingestion keys are random and stored only as SHA-256 hashes; comparison is constant-time. Keys can be rotated from the Projects screen and the old credential is revoked immediately. Administrative reads require `AGENTSCOPE_ADMIN_TOKEN` when demo mode is disabled. CORS is an explicit allowlist. Request and batch sizes are bounded. Logs never include request bodies. Core instrumentation does not capture prompts, completions, call arguments, or results. This preview does not yet implement distributed rate limiting or production dashboard login.

## Costs and diagnostics

Money uses `Decimal` and records a pricing version on each span. Unknown price or absent usage stays `null`, never zero. The bundled `demo/simulated-model` price is illustrative demo data, not current vendor pricing. Custom pricing versions are managed from Projects and apply only to newly ingested spans, preserving historical reproducibility.

- AS001 detects repeated tools using tool name plus a safe fingerprint; raw arguments are not retained for this rule.
- AS002 identifies a span occupying at least half of a trace critical path and at least 100 ms.
- AS003 requires ten historical token-bearing traces and flags usage at least twice the median.
- AS004 uses explicit retry attempt and operation metadata; it never guesses from span names.
- AS005 flags reported usage with no matching versioned price.

## Development and tests

```bash
.venv/bin/pytest
.venv/bin/ruff check backend sdk/python
cd frontend && npm run build
.venv/bin/python -m build sdk/python
.venv/bin/twine check sdk/python/dist/*
```

Architecture is a modular monolith: FastAPI ingestion/API/analytics modules share PostgreSQL through async SQLAlchemy; the separately packaged SDK exports OTLP; Next.js reads typed REST responses. See [architecture](docs/architecture/overview.md).

## Name check

On 2026-11-23, PyPI's official JSON endpoint for `agentscope-ai` returned 404. The public source repository is now `hanzales/agentscope-ai`; no package has been published. This remains a point-in-time availability check, not a trademark or uniqueness claim.

## Roadmap

Retention workers, richer time-series charts, verified optional provider adapters, production dashboard authentication, and stronger deployment controls.

MIT licensed. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
