# ADR 0001: Platform and application stack

- Status: accepted
- Date: 2026-07-28

## Context

The system needs Python-native image processing and model orchestration, a
private review interface, durable state, ephemeral cloud GPUs, and low idle
cost. It is initially operated by one creator.

## Decision

- Python 3.12
- FastAPI for API and server-rendered UI
- PostgreSQL for workflow state, outbox work, and audit records
- SQLAlchemy 2 and Alembic
- Pydantic Settings for typed environment configuration
- S3-compatible private object storage
- SaladCloud Container Engine and Job Queues
- ComfyUI API in a separately built, pinned GPU image
- pytest, Ruff, and mypy for verification
- Docker/Compose for deployable development and VPS services

The first dashboard will be server-rendered with narrowly scoped JavaScript for
keyboard review and comparisons. A standalone frontend is deferred unless its
benefit is demonstrated.

## Consequences

- Control-plane operation needs only one application runtime.
- Image and orchestration logic share typed Python domain models.
- PostgreSQL removes the need for a second always-on queue service initially.
- GPU images remain independent from control-plane releases.
- PostgreSQL-specific work claiming needs real PostgreSQL integration tests.
- Interactive UI behavior must remain intentionally small and accessible.

## Alternatives considered

- Next.js plus a Python API: capable but duplicates authentication, deployment,
  schemas, and observability for the initial scale.
- Forge as the production worker: optimized for interactive use rather than a
  versioned, headless workflow contract.
- Redis/Celery from day one: adds an always-on dependency before workload
  measurements justify it.
- Persistent GPU VM: easier interactively but wastes idle GPU spend and has a
  larger recovery surface.
- RunPod Pods or Serverless: technically viable and retained as a future
  provider adapter, but not selected for this workload. RunPod's current
  [Terms of Service](https://www.runpod.io/legal/terms-of-service) list
  pornography and graphic adult content as unauthorized content. A RunPod
  adapter therefore remains disabled unless a separate written agreement
  expressly permits the workload.
- SaladCloud provides a documented job-queue worker and scale-to-zero
  lifecycle. Generation specifications, signed upload grants, and the ComfyUI
  worker contract remain provider-neutral so the GPU backend can be replaced
  without changing raw-master identities.
