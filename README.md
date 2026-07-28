# Gen Automation

Cloud-native image generation, review, packaging, and publishing automation.

The system is designed around a small always-on control plane and ephemeral GPU
workers:

- FastAPI control plane and private review dashboard
- PostgreSQL as the authoritative workflow database
- S3-compatible object storage as the authoritative asset archive
- SaladCloud Job Queues and pinned ComfyUI workers for generation
- Deterministic, versioned generation specifications
- Versioned Forge-style prompt wildcards with seed-reproducible expansion
- Automated quality checks with human approval before publication
- Non-destructive watermarking and platform-specific derivatives
- An official X API transport and deterministic Patreon publishing handoff
- Private versioned archive storage, deterministic clean Patreon handoff
  packages, and restart-safe verified MEGA mirroring

## Repository status

The control plane, immutable raw-master storage, signed GPU worker, SaladCloud
orchestration, authentication/TOTP/RBAC, compliance registry, quality ranking,
ranked dashboard, durable human review, automatic derivative rendering,
restart-safe publication orchestration, Patreon package handoff, and concrete
X OAuth/posting path are implemented. Live infrastructure, provider
conformance, staging restore drills, and paid one-release canaries remain
deployment work. See:

- [MVP status and deployment handoff](docs/mvp-status.md)
- [Development plan](docs/development-plan.md)
- [Production rollout gates](docs/production-rollout.md)
- [Architecture](docs/architecture.md)
- [Data model and state machines](docs/data-model.md)
- [Object-storage contract](docs/object-storage.md)
- [Live object-storage conformance](docs/storage-conformance.md)
- [Deterministic derivative rendering](docs/derivative-rendering.md)
- [Prompt wildcard libraries](docs/prompt-wildcards.md)
- [X API transport contract](docs/x-api-transport.md)
- [Patreon publishing handoff](docs/patreon-handoff.md)
- [MEGA completed-set delivery](docs/mega-delivery.md)
- [Durable publication orchestration](docs/publication-orchestration.md)
- [Deployment contract](docs/deployment-contract.md)
- [SaladCloud integration contract](docs/salad-cloud.md)
- [Security and trust boundaries](docs/security.md)
- [Platform and stack decision](docs/adr/0001-platform-and-stack.md)
- [AWS staging destination](docs/adr/0002-aws-staging-destination.md)

## Core principles

1. Raw masters are immutable.
2. Generated files are never trusted to ephemeral GPU disks.
3. Every image is reproducible from stored model hashes, workflow, parameters,
   prompt, and seed.
4. Automated ranking narrows the review queue but never silently destroys work.
5. Publication is idempotent, auditable, and requires explicit approval.
6. Credentials are injected at deployment time and never committed.
7. Generation is restricted to lawful, clearly adult subjects and approved model
   licenses.

## Development

Python 3.12 is required.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn gen_automation.app:app --reload
```

Run the complete verification suite:

```powershell
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m pytest --cov=gen_automation
```

Alternatively, `docker compose up --build` starts PostgreSQL, applies migrations,
and serves the control plane only on `http://127.0.0.1:8000`. Compose enables
the synthetic local-owner bypass explicitly for local development; never copy
that bypass setting into staging or production.

In an authenticated deployment, operators sign in at `/login` and use
`/dashboard` for the ranked release index. Owners and reviewers can open
short-lived, exact-version raw-master previews and downloads; administrator and
publisher roles cannot obtain raw-master URLs.

## GPU worker deployment

The GPU worker is a separate, ephemeral Salad container. Its `GEN_WORKER_*`
configuration and credentials are not needed for local control-plane development;
they are created and injected only when the staging GPU deployment is ready for
its paid canary.

The image starts pinned ComfyUI from `/opt/comfyui`, materializes approved
checkpoints and LoRAs from a private read-only S3-compatible artifact source, and
serves its queue target at `POST /jobs/generate` on port `8000`. See the
[SaladCloud integration contract](docs/salad-cloud.md) for the complete runtime
setting, path, health-probe, manifest-integrity, and secret-injection contract.
