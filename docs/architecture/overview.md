# Architecture

The backend is a modular monolith. `ingestion` parses protocol data without database concerns; `storage` owns the async engine; `analytics` owns reproducible cost logic; `diagnostics` owns deterministic rules; `api` composes them. Stable `(project_id, trace_id, span_id)` uniqueness provides retry idempotency. Trace envelopes expand as partial and out-of-order spans arrive. Parent IDs are references by identity, not foreign keys, so incomplete traces remain valid.

PostgreSQL is the deployment database. SQLite is supported for local development. Deleting a project cascades through traces, spans, findings, budgets, and alert state. Each project has a retention period and deterministic trace sample rate. Retention supports a dry-run count and requires an explicit authenticated execution; operators can schedule that endpoint after exporting required records. Error traces bypass sampling so failures are retained.
