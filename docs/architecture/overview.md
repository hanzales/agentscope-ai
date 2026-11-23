# Architecture

The backend is a modular monolith. `ingestion` parses protocol data without database concerns; `storage` owns the async engine; `analytics` owns reproducible cost logic; `diagnostics` owns deterministic rules; `api` composes them. Stable `(project_id, trace_id, span_id)` uniqueness provides retry idempotency. Trace envelopes expand as partial and out-of-order spans arrive. Parent IDs are references by identity, not foreign keys, so incomplete traces remain valid.

PostgreSQL is the deployment database. SQLite is supported for local tests. Deleting a project cascades through traces, spans, and findings. The configured retention period is declarative in this preview; automated deletion is not yet implemented. Production operators should schedule project-scoped deletion only after exporting required records.

