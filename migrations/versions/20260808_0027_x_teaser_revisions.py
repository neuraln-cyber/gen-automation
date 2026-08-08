"""Add atomic, append-only X teaser revisions.

Revision ID: 20260808_0027
Revises: 20260808_0026
Create Date: 2026-08-08
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260808_0027"
down_revision: str | None = "20260808_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_REVISION_NAMESPACE = UUID("aee253b7-ec6a-4daa-a599-308243872f57")
_HEAD_NAMESPACE = UUID("a1441524-9265-43ab-95f3-dd41f9a01353")
_MEMBER_NAMESPACE = UUID("b5247c17-ecde-4bc5-897a-1f9e8e78bb85")
_JOB_TRIGGERS = (
    "derivative_jobs_guard_insert",
    "derivative_jobs_guard_update",
    "derivative_jobs_reject_delete",
    "derivative_jobs_promote_release_after_success",
)


_SQLITE_JOB_INSERT = """
CREATE TRIGGER derivative_jobs_guard_insert
BEFORE INSERT ON derivative_jobs BEGIN
SELECT CASE WHEN NOT EXISTS (
SELECT 1 FROM release_selections AS selection
JOIN derivative_recipes AS recipe ON recipe.id = NEW.derivative_recipe_id
JOIN release_versions AS version ON version.id = NEW.release_version_id
JOIN releases AS release ON release.id = version.release_id
WHERE selection.id = NEW.release_selection_id
AND selection.release_version_id = NEW.release_version_id
AND recipe.release_version_id = NEW.release_version_id
AND recipe.expected_output_count = NEW.expected_output_count
AND release.current_version_no = version.version_no
AND ((NEW.gates_release = 1 AND release.phase = 'rendering') OR
(NEW.gates_release = 0 AND release.phase IN
('rendering', 'ready_to_publish', 'publishing', 'published')
AND EXISTS (SELECT 1 FROM x_teaser_revision_heads AS head
WHERE head.review_task_id = selection.review_task_id
AND head.release_version_id = NEW.release_version_id
AND (head.active_revision_id IS NOT NULL OR release.phase <> 'rendering')
AND head.pending_revision_id = NEW.x_teaser_revision_id)))
) THEN RAISE(ABORT, 'derivative job release snapshot is invalid') END; END
"""

_SQLITE_JOB_UPDATE = """
CREATE TRIGGER derivative_jobs_guard_update
BEFORE UPDATE ON derivative_jobs BEGIN
SELECT CASE WHEN OLD.state IN ('succeeded', 'cancelled')
OR NEW.lock_version <> OLD.lock_version + 1
OR OLD.id IS NOT NEW.id
OR OLD.release_selection_id IS NOT NEW.release_selection_id
OR OLD.derivative_recipe_id IS NOT NEW.derivative_recipe_id
OR OLD.x_teaser_revision_id IS NOT NEW.x_teaser_revision_id
OR OLD.gates_release IS NOT NEW.gates_release
OR OLD.release_version_id IS NOT NEW.release_version_id
OR OLD.logical_key IS NOT NEW.logical_key
OR OLD.request_payload IS NOT NEW.request_payload
OR OLD.request_sha256 IS NOT NEW.request_sha256
OR OLD.expected_output_count IS NOT NEW.expected_output_count
OR OLD.priority IS NOT NEW.priority
OR (OLD.max_attempts IS NOT NEW.max_attempts AND NOT (
OLD.state = 'failed' AND NEW.state = 'retry_wait'
AND NEW.max_attempts > OLD.max_attempts
AND NEW.max_attempts <= 10
AND NEW.max_attempts >= OLD.attempt_count + 1))
OR OLD.available_at IS NOT NEW.available_at
OR OLD.requested_at IS NOT NEW.requested_at
THEN RAISE(ABORT, 'derivative job identity is immutable') END;
SELECT CASE WHEN NOT (
(OLD.state IN ('requested', 'retry_wait') AND NEW.state IN ('claimed', 'cancelled'))
OR (OLD.state = 'claimed'
AND NEW.state IN ('claimed', 'processing', 'retry_wait', 'failed', 'cancelled'))
OR (OLD.state = 'processing'
AND NEW.state IN ('claimed', 'retry_wait', 'succeeded', 'failed', 'cancelled'))
OR (OLD.state = 'failed' AND NEW.state = 'retry_wait')
) THEN RAISE(ABORT, 'derivative job state transition is invalid') END;
SELECT CASE WHEN NEW.state = 'claimed'
AND NEW.attempt_count <> OLD.attempt_count + 1
THEN RAISE(ABORT, 'derivative job claim attempt is invalid') END;
SELECT CASE WHEN NEW.state <> 'claimed'
AND NEW.attempt_count <> OLD.attempt_count
THEN RAISE(ABORT, 'derivative job attempt count is immutable') END;
SELECT CASE WHEN OLD.state = 'failed' AND (
NEW.state <> 'retry_wait'
OR OLD.last_error_code IS 'output_object_conflict'
OR NEW.completed_at IS NOT NULL
OR NEW.last_error_code IS NOT NULL OR NEW.last_error_detail IS NOT NULL
OR NEW.lease_owner IS NOT NULL OR NEW.lease_expires_at IS NOT NULL
OR NEW.retry_at IS NULL
OR NEW.max_attempts <= OLD.max_attempts
OR NEW.max_attempts > 10
OR NEW.max_attempts < OLD.attempt_count + 1
OR NOT EXISTS (SELECT 1 FROM derivative_recipes AS retry_recipe
JOIN release_versions AS retry_version ON retry_version.id = OLD.release_version_id
JOIN releases AS retry_release ON retry_release.id = retry_version.release_id
WHERE retry_recipe.id = OLD.derivative_recipe_id
AND retry_recipe.release_version_id = OLD.release_version_id
AND json_array_length(retry_recipe.output_targets) = 1
AND json_extract(retry_recipe.output_targets, '$[0]') = 'full'
AND retry_release.current_version_no = retry_version.version_no
AND retry_release.phase = 'rendering'))
THEN RAISE(ABORT, 'failed derivative job rearm is invalid') END;
SELECT CASE WHEN OLD.state IN ('claimed', 'processing')
AND NEW.state = 'claimed'
AND (OLD.lease_expires_at IS NULL OR NEW.claimed_at < OLD.lease_expires_at)
THEN RAISE(ABORT, 'active derivative job lease cannot be stolen') END;
SELECT CASE WHEN NEW.state = 'succeeded' AND (
SELECT count(*) FROM derivative_outputs WHERE derivative_job_id = OLD.id
) <> OLD.expected_output_count
THEN RAISE(ABORT, 'derivative job outputs are incomplete') END;
SELECT CASE WHEN NEW.state = 'succeeded' AND NOT EXISTS (
SELECT 1 FROM release_versions AS version
JOIN releases AS release ON release.id = version.release_id
JOIN release_selections AS selection ON selection.id = OLD.release_selection_id
WHERE version.id = OLD.release_version_id
AND release.current_version_no = version.version_no
AND ((NEW.gates_release = 1 AND release.phase = 'rendering') OR
(NEW.gates_release = 0 AND release.phase IN
('rendering', 'ready_to_publish', 'publishing', 'published')
AND EXISTS (SELECT 1 FROM x_teaser_revision_heads AS head
WHERE head.review_task_id = selection.review_task_id
AND head.release_version_id = NEW.release_version_id
AND (head.active_revision_id IS NOT NULL OR release.phase <> 'rendering')
AND head.pending_revision_id = NEW.x_teaser_revision_id))))
THEN RAISE(ABORT, 'derivative job release phase is invalid') END;
END
"""

_SQLITE_JOB_DELETE = """
CREATE TRIGGER derivative_jobs_reject_delete BEFORE DELETE ON derivative_jobs
BEGIN SELECT RAISE(ABORT, 'derivative jobs cannot be deleted'); END
"""

_SQLITE_JOB_PROMOTE = """
CREATE TRIGGER derivative_jobs_promote_release_after_success
AFTER UPDATE ON derivative_jobs
WHEN OLD.state <> 'succeeded' AND NEW.state = 'succeeded'
AND NEW.gates_release = 1
AND NOT EXISTS (SELECT 1 FROM derivative_jobs AS pending
WHERE pending.release_version_id = NEW.release_version_id
AND pending.gates_release = 1 AND pending.state <> 'succeeded'
AND ((pending.x_teaser_revision_id IS NULL AND (
EXISTS (SELECT 1 FROM derivative_recipes AS pending_recipe
WHERE pending_recipe.id = pending.derivative_recipe_id
AND EXISTS (SELECT 1 FROM json_each(pending_recipe.output_targets) AS pending_target
WHERE pending_target.value = 'full'))
OR NOT EXISTS (SELECT 1 FROM release_selections AS pending_selection
JOIN x_teaser_revision_heads AS pending_head
ON pending_head.review_task_id = pending_selection.review_task_id
WHERE pending_selection.id = pending.release_selection_id
AND pending_head.release_version_id = pending.release_version_id)))
OR (pending.x_teaser_revision_id IS NOT NULL AND EXISTS (
SELECT 1 FROM release_selections AS pending_selection
JOIN x_teaser_revision_heads AS pending_head
ON pending_head.review_task_id = pending_selection.review_task_id
WHERE pending_selection.id = pending.release_selection_id
AND pending_head.release_version_id = pending.release_version_id
AND (pending_head.active_revision_id = pending.x_teaser_revision_id
OR pending_head.pending_revision_id = pending.x_teaser_revision_id))))) BEGIN
UPDATE releases SET phase = 'ready_to_publish', lock_version = lock_version + 1
WHERE phase = 'rendering' AND id = (
SELECT version.release_id FROM release_versions AS version
WHERE version.id = NEW.release_version_id
AND version.version_no = releases.current_version_no);
SELECT CASE WHEN changes() <> 1
THEN RAISE(ABORT, 'release readiness compare-and-swap failed') END; END
"""

_SQLITE_LEGACY_JOB_INSERT = _SQLITE_JOB_INSERT.replace(
    "AND ((NEW.gates_release = 1 AND release.phase = 'rendering') OR\n"
    "(NEW.gates_release = 0 AND release.phase IN\n"
    "('rendering', 'ready_to_publish', 'publishing', 'published')\n"
    "AND EXISTS (SELECT 1 FROM x_teaser_revision_heads AS head\n"
    "WHERE head.review_task_id = selection.review_task_id\n"
    "AND head.release_version_id = NEW.release_version_id\n"
    "AND (head.active_revision_id IS NOT NULL OR release.phase <> 'rendering')\n"
    "AND head.pending_revision_id = NEW.x_teaser_revision_id)))",
    "AND release.phase = 'rendering'",
)
_SQLITE_LEGACY_JOB_UPDATE = (
    _SQLITE_JOB_UPDATE.replace("OR OLD.x_teaser_revision_id IS NOT NEW.x_teaser_revision_id\n", "")
    .replace("OR OLD.gates_release IS NOT NEW.gates_release\n", "")
    .replace(
        "SELECT CASE WHEN NEW.state = 'succeeded' AND NOT EXISTS (\n"
        "SELECT 1 FROM release_versions AS version\n"
        "JOIN releases AS release ON release.id = version.release_id\n"
        "JOIN release_selections AS selection ON selection.id = OLD.release_selection_id\n"
        "WHERE version.id = OLD.release_version_id\n"
        "AND release.current_version_no = version.version_no\n"
        "AND ((NEW.gates_release = 1 AND release.phase = 'rendering') OR\n"
        "(NEW.gates_release = 0 AND release.phase IN\n"
        "('rendering', 'ready_to_publish', 'publishing', 'published')\n"
        "AND EXISTS (SELECT 1 FROM x_teaser_revision_heads AS head\n"
        "WHERE head.review_task_id = selection.review_task_id\n"
        "AND head.release_version_id = NEW.release_version_id\n"
        "AND (head.active_revision_id IS NOT NULL OR release.phase <> 'rendering')\n"
        "AND head.pending_revision_id = NEW.x_teaser_revision_id))))\n"
        "THEN RAISE(ABORT, 'derivative job release phase is invalid') END;",
        "SELECT CASE WHEN NEW.state = 'succeeded' AND NOT EXISTS (\n"
        "SELECT 1 FROM release_versions AS version\n"
        "JOIN releases AS release ON release.id = version.release_id\n"
        "WHERE version.id = OLD.release_version_id\n"
        "AND release.current_version_no = version.version_no\n"
        "AND release.phase = 'rendering')\n"
        "THEN RAISE(ABORT, 'derivative job release phase is invalid') END;",
    )
)
_SQLITE_LEGACY_JOB_PROMOTE = """
CREATE TRIGGER derivative_jobs_promote_release_after_success
AFTER UPDATE ON derivative_jobs
WHEN OLD.state <> 'succeeded' AND NEW.state = 'succeeded'
AND NOT EXISTS (SELECT 1 FROM derivative_jobs AS pending
WHERE pending.release_version_id = NEW.release_version_id
AND pending.state <> 'succeeded') BEGIN
UPDATE releases SET phase = 'ready_to_publish', lock_version = lock_version + 1
WHERE phase = 'rendering' AND id = (
SELECT version.release_id FROM release_versions AS version
WHERE version.id = NEW.release_version_id
AND version.version_no = releases.current_version_no);
SELECT CASE WHEN changes() <> 1
THEN RAISE(ABORT, 'release readiness compare-and-swap failed') END; END
"""


_POSTGRES_JOB_FUNCTIONS = (
    """
CREATE OR REPLACE FUNCTION gen_automation_guard_derivative_job_insert()
RETURNS trigger AS $$ BEGIN
IF NOT EXISTS (SELECT 1 FROM release_selections AS selection
JOIN derivative_recipes AS recipe ON recipe.id = NEW.derivative_recipe_id
JOIN release_versions AS version ON version.id = NEW.release_version_id
JOIN releases AS release ON release.id = version.release_id
WHERE selection.id = NEW.release_selection_id
AND selection.release_version_id = NEW.release_version_id
AND recipe.release_version_id = NEW.release_version_id
AND recipe.expected_output_count = NEW.expected_output_count
AND release.current_version_no = version.version_no
AND ((NEW.gates_release AND release.phase = 'rendering') OR
(NOT NEW.gates_release AND release.phase IN
('rendering', 'ready_to_publish', 'publishing', 'published')
AND EXISTS (SELECT 1 FROM x_teaser_revision_heads AS head
WHERE head.review_task_id = selection.review_task_id
AND head.release_version_id = NEW.release_version_id
AND (head.active_revision_id IS NOT NULL OR release.phase <> 'rendering')
AND head.pending_revision_id = NEW.x_teaser_revision_id)))) THEN
RAISE EXCEPTION 'derivative job release snapshot is invalid'; END IF;
RETURN NEW; END; $$ LANGUAGE plpgsql
""",
    """
CREATE OR REPLACE FUNCTION gen_automation_guard_derivative_job_mutation()
RETURNS trigger AS $$ BEGIN
IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'derivative jobs cannot be deleted'; END IF;
IF OLD.state IN ('succeeded', 'cancelled')
OR NEW.lock_version <> OLD.lock_version + 1
OR OLD.id IS DISTINCT FROM NEW.id
OR OLD.release_selection_id IS DISTINCT FROM NEW.release_selection_id
OR OLD.derivative_recipe_id IS DISTINCT FROM NEW.derivative_recipe_id
OR OLD.x_teaser_revision_id IS DISTINCT FROM NEW.x_teaser_revision_id
OR OLD.gates_release IS DISTINCT FROM NEW.gates_release
OR OLD.release_version_id IS DISTINCT FROM NEW.release_version_id
OR OLD.logical_key IS DISTINCT FROM NEW.logical_key
OR OLD.request_payload IS DISTINCT FROM NEW.request_payload
OR OLD.request_sha256 IS DISTINCT FROM NEW.request_sha256
OR OLD.expected_output_count IS DISTINCT FROM NEW.expected_output_count
OR OLD.priority IS DISTINCT FROM NEW.priority
OR (OLD.max_attempts IS DISTINCT FROM NEW.max_attempts AND NOT (
OLD.state = 'failed' AND NEW.state = 'retry_wait'
AND NEW.max_attempts > OLD.max_attempts AND NEW.max_attempts <= 10
AND NEW.max_attempts >= OLD.attempt_count + 1))
OR OLD.available_at IS DISTINCT FROM NEW.available_at
OR OLD.requested_at IS DISTINCT FROM NEW.requested_at THEN
RAISE EXCEPTION 'derivative job identity is immutable'; END IF;
IF NOT ((OLD.state IN ('requested', 'retry_wait') AND NEW.state IN ('claimed', 'cancelled'))
OR (OLD.state = 'claimed' AND NEW.state IN
('claimed', 'processing', 'retry_wait', 'failed', 'cancelled'))
OR (OLD.state = 'processing' AND NEW.state IN
('claimed', 'retry_wait', 'succeeded', 'failed', 'cancelled'))
OR (OLD.state = 'failed' AND NEW.state = 'retry_wait')) THEN
RAISE EXCEPTION 'derivative job state transition is invalid'; END IF;
IF NEW.state = 'claimed' AND NEW.attempt_count <> OLD.attempt_count + 1 THEN
RAISE EXCEPTION 'derivative job claim attempt is invalid'; END IF;
IF NEW.state <> 'claimed' AND NEW.attempt_count <> OLD.attempt_count THEN
RAISE EXCEPTION 'derivative job attempt count is immutable'; END IF;
IF OLD.state = 'failed' AND (NEW.state <> 'retry_wait'
OR OLD.last_error_code IS NOT DISTINCT FROM 'output_object_conflict'
OR NEW.completed_at IS NOT NULL OR NEW.last_error_code IS NOT NULL
OR NEW.last_error_detail IS NOT NULL OR NEW.lease_owner IS NOT NULL
OR NEW.lease_expires_at IS NOT NULL OR NEW.retry_at IS NULL
OR NEW.max_attempts <= OLD.max_attempts OR NEW.max_attempts > 10
OR NEW.max_attempts < OLD.attempt_count + 1
OR NOT EXISTS (SELECT 1 FROM derivative_recipes AS retry_recipe
JOIN release_versions AS retry_version ON retry_version.id = OLD.release_version_id
JOIN releases AS retry_release ON retry_release.id = retry_version.release_id
WHERE retry_recipe.id = OLD.derivative_recipe_id
AND retry_recipe.release_version_id = OLD.release_version_id
AND retry_recipe.output_targets::jsonb = '["full"]'::jsonb
AND retry_release.current_version_no = retry_version.version_no
AND retry_release.phase = 'rendering')) THEN
RAISE EXCEPTION 'failed derivative job rearm is invalid'; END IF;
IF OLD.state IN ('claimed', 'processing') AND NEW.state = 'claimed'
AND (OLD.lease_expires_at IS NULL OR NEW.claimed_at < OLD.lease_expires_at) THEN
RAISE EXCEPTION 'active derivative job lease cannot be stolen'; END IF;
IF NEW.state = 'succeeded' AND (SELECT count(*) FROM derivative_outputs
WHERE derivative_job_id = OLD.id) <> OLD.expected_output_count THEN
RAISE EXCEPTION 'derivative job outputs are incomplete'; END IF;
IF NEW.state = 'succeeded' AND NOT EXISTS (
SELECT 1 FROM release_versions AS version
JOIN releases AS release ON release.id = version.release_id
JOIN release_selections AS selection ON selection.id = OLD.release_selection_id
WHERE version.id = OLD.release_version_id
AND release.current_version_no = version.version_no
AND ((NEW.gates_release AND release.phase = 'rendering') OR
(NOT NEW.gates_release AND release.phase IN
('rendering', 'ready_to_publish', 'publishing', 'published')
AND EXISTS (SELECT 1 FROM x_teaser_revision_heads AS head
WHERE head.review_task_id = selection.review_task_id
AND head.release_version_id = NEW.release_version_id
AND (head.active_revision_id IS NOT NULL OR release.phase <> 'rendering')
AND head.pending_revision_id = NEW.x_teaser_revision_id)))) THEN
RAISE EXCEPTION 'derivative job release phase is invalid'; END IF;
RETURN NEW; END; $$ LANGUAGE plpgsql
""",
    """
CREATE OR REPLACE FUNCTION gen_automation_promote_rendered_release()
RETURNS trigger AS $$ DECLARE changed_count integer; BEGIN
IF OLD.state <> 'succeeded' AND NEW.state = 'succeeded' AND NEW.gates_release
AND NOT EXISTS (SELECT 1 FROM derivative_jobs AS pending
WHERE pending.release_version_id = NEW.release_version_id
AND pending.gates_release AND pending.state <> 'succeeded'
AND ((pending.x_teaser_revision_id IS NULL AND (
EXISTS (SELECT 1 FROM derivative_recipes AS pending_recipe
WHERE pending_recipe.id = pending.derivative_recipe_id
AND pending_recipe.output_targets::jsonb ? 'full')
OR NOT EXISTS (SELECT 1 FROM release_selections AS pending_selection
JOIN x_teaser_revision_heads AS pending_head
ON pending_head.review_task_id = pending_selection.review_task_id
WHERE pending_selection.id = pending.release_selection_id
AND pending_head.release_version_id = pending.release_version_id)))
OR (pending.x_teaser_revision_id IS NOT NULL AND EXISTS (
SELECT 1 FROM release_selections AS pending_selection
JOIN x_teaser_revision_heads AS pending_head
ON pending_head.review_task_id = pending_selection.review_task_id
WHERE pending_selection.id = pending.release_selection_id
AND pending_head.release_version_id = pending.release_version_id
AND (pending_head.active_revision_id = pending.x_teaser_revision_id
OR pending_head.pending_revision_id = pending.x_teaser_revision_id))))) THEN
UPDATE releases SET phase = 'ready_to_publish', lock_version = lock_version + 1
WHERE phase = 'rendering' AND id = (
SELECT version.release_id FROM release_versions AS version
WHERE version.id = NEW.release_version_id
AND version.version_no = releases.current_version_no);
GET DIAGNOSTICS changed_count = ROW_COUNT;
IF changed_count <> 1 THEN
RAISE EXCEPTION 'release readiness compare-and-swap failed'; END IF;
END IF; RETURN NEW; END; $$ LANGUAGE plpgsql
""",
)


def upgrade() -> None:
    bind = op.get_bind()
    _create_revision_tables()
    dependent_trigger_sql: tuple[str, ...] = ()
    if bind.dialect.name == "sqlite":
        dependent_trigger_sql = _drop_sqlite_dependent_job_triggers(bind)
        _drop_sqlite_job_triggers()
    else:
        _drop_postgresql_job_triggers()
    op.add_column(
        "derivative_jobs",
        sa.Column("x_teaser_revision_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "derivative_jobs",
        sa.Column("gates_release", sa.Boolean(), nullable=True),
    )
    op.execute(sa.text("UPDATE derivative_jobs SET gates_release = true"))
    with op.batch_alter_table("derivative_jobs") as batch_op:
        batch_op.alter_column("gates_release", existing_type=sa.Boolean(), nullable=False)
        batch_op.drop_constraint("uq_derivative_jobs_selection_recipe", type_="unique")
        batch_op.create_foreign_key(
            "fk_derivative_jobs_x_teaser_revision_id_x_teaser_revisions",
            "x_teaser_revisions",
            ["x_teaser_revision_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_derivative_jobs_nongating_job_requires_x_revision",
            "gates_release = true OR x_teaser_revision_id IS NOT NULL",
        )
    op.create_index(
        op.f("ix_derivative_jobs_x_teaser_revision_id"),
        "derivative_jobs",
        ["x_teaser_revision_id"],
    )
    op.create_index(
        "uq_derivative_jobs_selection_recipe_legacy",
        "derivative_jobs",
        ["release_selection_id", "derivative_recipe_id"],
        unique=True,
        sqlite_where=sa.text("x_teaser_revision_id IS NULL"),
        postgresql_where=sa.text("x_teaser_revision_id IS NULL"),
    )
    op.create_index(
        "uq_derivative_jobs_selection_recipe_revision",
        "derivative_jobs",
        ["release_selection_id", "derivative_recipe_id", "x_teaser_revision_id"],
        unique=True,
        sqlite_where=sa.text("x_teaser_revision_id IS NOT NULL"),
        postgresql_where=sa.text("x_teaser_revision_id IS NOT NULL"),
    )
    _create_revision_members()
    _backfill_completed_legacy_revisions(bind)
    if bind.dialect.name == "sqlite":
        _create_sqlite_revision_triggers()
        for statement in (
            _SQLITE_JOB_INSERT,
            _SQLITE_JOB_UPDATE,
            _SQLITE_JOB_DELETE,
            _SQLITE_JOB_PROMOTE,
        ):
            op.execute(statement)
        for statement in dependent_trigger_sql:
            op.execute(statement)
    else:
        _create_postgresql_revision_guards()
        for statement in _POSTGRES_JOB_FUNCTIONS:
            op.execute(statement)
        _create_postgresql_job_triggers()


def downgrade() -> None:
    bind = op.get_bind()
    pending_head = bind.execute(
        sa.text(
            "SELECT review_task_id FROM x_teaser_revision_heads "
            "WHERE pending_revision_id IS NOT NULL LIMIT 1"
        )
    ).first()
    if pending_head is not None:
        raise RuntimeError("cannot downgrade X teaser revisions while a revision is pending")
    revision_history = bind.execute(
        sa.text("SELECT id FROM x_teaser_revisions WHERE revision_no > 1 LIMIT 1")
    ).first()
    if revision_history is not None:
        raise RuntimeError(
            "cannot downgrade X teaser revisions without losing active revision history"
        )
    duplicate_job = bind.execute(
        sa.text(
            "SELECT release_selection_id, derivative_recipe_id "
            "FROM derivative_jobs GROUP BY release_selection_id, derivative_recipe_id "
            "HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate_job is not None:
        raise RuntimeError(
            "cannot downgrade X teaser revisions without losing derivative jobs: "
            "multiple revisions use the same selection and recipe"
        )
    nonterminal_revision_job = bind.execute(
        sa.text(
            "SELECT id FROM derivative_jobs WHERE x_teaser_revision_id IS NOT NULL "
            "AND state NOT IN ('succeeded', 'failed', 'cancelled') LIMIT 1"
        )
    ).first()
    if nonterminal_revision_job is not None:
        raise RuntimeError(
            "cannot downgrade X teaser revisions while revision jobs are nonterminal"
        )
    if bind.dialect.name == "postgresql":
        ambiguous_selection_query = sa.text(
            "SELECT job.release_selection_id FROM derivative_jobs AS job "
            "JOIN derivative_recipes AS recipe ON recipe.id = job.derivative_recipe_id "
            "WHERE recipe.output_targets::jsonb = '[\"x_teaser\"]'::jsonb "
            "GROUP BY job.release_selection_id HAVING count(*) > 1 LIMIT 1"
        )
    else:
        ambiguous_selection_query = sa.text(
            "SELECT job.release_selection_id FROM derivative_jobs AS job "
            "JOIN derivative_recipes AS recipe ON recipe.id = job.derivative_recipe_id "
            "WHERE json_array_length(recipe.output_targets) = 1 "
            "AND json_extract(recipe.output_targets, '$[0]') = 'x_teaser' "
            "GROUP BY job.release_selection_id HAVING count(*) > 1 LIMIT 1"
        )
    ambiguous_selection = bind.execute(ambiguous_selection_query).first()
    if ambiguous_selection is not None:
        raise RuntimeError(
            "cannot downgrade X teaser revisions without losing the canonical X output: "
            "multiple X jobs use the same release selection"
        )
    dependent_trigger_sql: tuple[str, ...] = ()
    if bind.dialect.name == "sqlite":
        dependent_trigger_sql = _drop_sqlite_dependent_job_triggers(bind)
        _drop_sqlite_job_triggers()
    else:
        for statement in _legacy_postgresql_job_functions():
            op.execute(statement)
    _drop_revision_triggers(bind.dialect.name)
    op.drop_table("x_teaser_revision_members")
    op.drop_index(
        "uq_derivative_jobs_selection_recipe_revision",
        table_name="derivative_jobs",
    )
    op.drop_index(
        "uq_derivative_jobs_selection_recipe_legacy",
        table_name="derivative_jobs",
    )
    op.drop_index(
        op.f("ix_derivative_jobs_x_teaser_revision_id"),
        table_name="derivative_jobs",
    )
    with op.batch_alter_table("derivative_jobs") as batch_op:
        batch_op.drop_constraint(
            "ck_derivative_jobs_nongating_job_requires_x_revision",
            type_="check",
        )
        batch_op.drop_constraint(
            "fk_derivative_jobs_x_teaser_revision_id_x_teaser_revisions",
            type_="foreignkey",
        )
        batch_op.drop_column("gates_release")
        batch_op.drop_column("x_teaser_revision_id")
        batch_op.create_unique_constraint(
            "uq_derivative_jobs_selection_recipe",
            ["release_selection_id", "derivative_recipe_id"],
        )
    op.drop_table("x_teaser_revision_heads")
    op.drop_table("x_teaser_revisions")
    if bind.dialect.name == "sqlite":
        for statement in (
            _SQLITE_LEGACY_JOB_INSERT,
            _SQLITE_LEGACY_JOB_UPDATE,
            _SQLITE_JOB_DELETE,
            _SQLITE_LEGACY_JOB_PROMOTE,
        ):
            op.execute(statement)
        for statement in dependent_trigger_sql:
            op.execute(statement)


def _create_revision_tables() -> None:
    op.create_table(
        "x_teaser_revisions",
        sa.Column("review_task_id", sa.Uuid(), nullable=False),
        sa.Column("release_version_id", sa.Uuid(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("watermark_asset_id", sa.Uuid(), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("revision_no > 0", name="ck_x_teaser_revisions_positive_revision"),
        sa.CheckConstraint(
            "length(request_sha256) = 64",
            name="ck_x_teaser_revisions_valid_request_sha256",
        ),
        sa.ForeignKeyConstraint(["review_task_id"], ["review_tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["release_version_id"], ["release_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["watermark_asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["admin_users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "review_task_id", "revision_no", name="uq_x_teaser_revisions_task_revision"
        ),
        sa.UniqueConstraint(
            "id",
            "review_task_id",
            "release_version_id",
            name="uq_x_teaser_revisions_identity",
        ),
    )
    op.create_index(
        op.f("ix_x_teaser_revisions_review_task_id"),
        "x_teaser_revisions",
        ["review_task_id"],
    )
    op.create_index(
        op.f("ix_x_teaser_revisions_release_version_id"),
        "x_teaser_revisions",
        ["release_version_id"],
    )
    op.create_index(
        "ix_x_teaser_revisions_task_created",
        "x_teaser_revisions",
        ["review_task_id", "created_at"],
    )
    op.create_table(
        "x_teaser_revision_heads",
        sa.Column("review_task_id", sa.Uuid(), nullable=False),
        sa.Column("release_version_id", sa.Uuid(), nullable=False),
        sa.Column("active_revision_id", sa.Uuid(), nullable=True),
        sa.Column("pending_revision_id", sa.Uuid(), nullable=True),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "lock_version > 0", name="ck_x_teaser_revision_heads_positive_lock_version"
        ),
        sa.CheckConstraint(
            "active_revision_id IS NULL OR pending_revision_id IS NULL "
            "OR active_revision_id <> pending_revision_id",
            name="ck_x_teaser_revision_heads_distinct_revision_pointers",
        ),
        sa.ForeignKeyConstraint(["review_task_id"], ["review_tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["release_version_id"], ["release_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["active_revision_id"], ["x_teaser_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["pending_revision_id"], ["x_teaser_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["active_revision_id", "review_task_id", "release_version_id"],
            [
                "x_teaser_revisions.id",
                "x_teaser_revisions.review_task_id",
                "x_teaser_revisions.release_version_id",
            ],
            name="fk_x_teaser_revision_heads_active_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["pending_revision_id", "review_task_id", "release_version_id"],
            [
                "x_teaser_revisions.id",
                "x_teaser_revisions.review_task_id",
                "x_teaser_revisions.release_version_id",
            ],
            name="fk_x_teaser_revision_heads_pending_identity",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("review_task_id", name="uq_x_teaser_revision_heads_review_task"),
    )
    op.create_index(
        op.f("ix_x_teaser_revision_heads_review_task_id"),
        "x_teaser_revision_heads",
        ["review_task_id"],
    )
    op.create_index(
        op.f("ix_x_teaser_revision_heads_release_version_id"),
        "x_teaser_revision_heads",
        ["release_version_id"],
    )


def _create_revision_members() -> None:
    op.create_table(
        "x_teaser_revision_members",
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("review_task_id", sa.Uuid(), nullable=False),
        sa.Column("release_version_id", sa.Uuid(), nullable=False),
        sa.Column("release_selection_id", sa.Uuid(), nullable=False),
        sa.Column("source_asset_id", sa.Uuid(), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("watermark_position", sa.String(length=20), nullable=False),
        sa.Column("derivative_recipe_id", sa.Uuid(), nullable=False),
        sa.Column("derivative_job_id", sa.Uuid(), nullable=True),
        sa.Column("derivative_output_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "display_order > 0",
            name="ck_x_teaser_revision_members_positive_display_order",
        ),
        sa.CheckConstraint(
            "watermark_position IN ('top_left', 'top_right', 'bottom_left', 'bottom_right')",
            name="ck_x_teaser_revision_members_valid_watermark_position",
        ),
        sa.CheckConstraint(
            "(derivative_job_id IS NOT NULL AND derivative_output_id IS NULL) OR "
            "(derivative_job_id IS NULL AND derivative_output_id IS NOT NULL)",
            name="ck_x_teaser_revision_members_job_or_reused_output",
        ),
        sa.ForeignKeyConstraint(["revision_id"], ["x_teaser_revisions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["review_task_id"], ["review_tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["release_version_id"], ["release_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["release_selection_id"], ["release_selections.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["source_asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["derivative_recipe_id"], ["derivative_recipes.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["derivative_job_id"], ["derivative_jobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["derivative_output_id"], ["derivative_outputs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["revision_id", "review_task_id", "release_version_id"],
            [
                "x_teaser_revisions.id",
                "x_teaser_revisions.review_task_id",
                "x_teaser_revisions.release_version_id",
            ],
            name="fk_x_teaser_revision_members_revision_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["release_selection_id", "source_asset_id"],
            ["release_selections.id", "release_selections.asset_id"],
            name="fk_x_teaser_revision_members_selection_source",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "revision_id",
            "release_selection_id",
            name="uq_x_teaser_revision_members_selection",
        ),
        sa.UniqueConstraint(
            "revision_id", "display_order", name="uq_x_teaser_revision_members_order"
        ),
    )
    for column in (
        "revision_id",
        "release_selection_id",
        "derivative_job_id",
        "derivative_output_id",
    ):
        op.create_index(
            op.f(f"ix_x_teaser_revision_members_{column}"),
            "x_teaser_revision_members",
            [column],
        )
    op.create_index(
        "ix_x_teaser_revision_members_revision_order",
        "x_teaser_revision_members",
        ["revision_id", "display_order"],
    )


def _backfill_completed_legacy_revisions(bind: sa.Connection) -> None:
    selections = sa.table(
        "release_selections",
        sa.column("id", sa.Uuid()),
        sa.column("review_task_id", sa.Uuid()),
        sa.column("release_version_id", sa.Uuid()),
        sa.column("asset_id", sa.Uuid()),
        sa.column("display_order", sa.Integer()),
    )
    x_selections = sa.table(
        "review_x_selections",
        sa.column("review_task_id", sa.Uuid()),
        sa.column("asset_id", sa.Uuid()),
    )
    jobs = sa.table(
        "derivative_jobs",
        sa.column("id", sa.Uuid()),
        sa.column("release_selection_id", sa.Uuid()),
        sa.column("derivative_recipe_id", sa.Uuid()),
        sa.column("state", sa.String()),
    )
    outputs = sa.table(
        "derivative_outputs",
        sa.column("id", sa.Uuid()),
        sa.column("derivative_job_id", sa.Uuid()),
        sa.column("target", sa.String()),
        sa.column("recorded_at", sa.DateTime(timezone=True)),
    )
    recipes = sa.table(
        "derivative_recipes",
        sa.column("id", sa.Uuid()),
        sa.column("watermark_asset_id", sa.Uuid()),
        sa.column("configuration", json_type),
        sa.column("created_by_user_id", sa.Uuid()),
    )
    selected_rows = list(
        bind.execute(
            sa.select(
                selections.c.id,
                selections.c.review_task_id,
                selections.c.release_version_id,
                selections.c.asset_id,
                selections.c.display_order,
            )
            .join(
                x_selections,
                (x_selections.c.review_task_id == selections.c.review_task_id)
                & (x_selections.c.asset_id == selections.c.asset_id),
            )
            .order_by(selections.c.review_task_id, selections.c.display_order)
        ).mappings()
    )
    candidates = list(
        bind.execute(
            sa.select(
                selections.c.review_task_id,
                selections.c.id.label("release_selection_id"),
                outputs.c.id.label("output_id"),
                outputs.c.recorded_at,
                jobs.c.derivative_recipe_id,
                recipes.c.watermark_asset_id,
                recipes.c.configuration,
                recipes.c.created_by_user_id,
            )
            .join(jobs, jobs.c.release_selection_id == selections.c.id)
            .join(outputs, outputs.c.derivative_job_id == jobs.c.id)
            .join(recipes, recipes.c.id == jobs.c.derivative_recipe_id)
            .join(
                x_selections,
                (x_selections.c.review_task_id == selections.c.review_task_id)
                & (x_selections.c.asset_id == selections.c.asset_id),
            )
            .where(
                jobs.c.state == "succeeded",
                outputs.c.target == "x_teaser",
                recipes.c.watermark_asset_id.is_not(None),
            )
            .order_by(
                selections.c.review_task_id,
                selections.c.id,
                outputs.c.recorded_at.desc(),
                outputs.c.id.desc(),
            )
        ).mappings()
    )
    selected_by_task: dict[UUID, list[Mapping[str, Any]]] = defaultdict(list)
    for row in selected_rows:
        selected_by_task[row["review_task_id"]].append(row)
    candidate_by_selection: dict[UUID, Mapping[str, Any]] = {}
    for row in candidates:
        candidate_by_selection.setdefault(row["release_selection_id"], row)

    revisions = sa.table(
        "x_teaser_revisions",
        sa.column("id", sa.Uuid()),
        sa.column("review_task_id", sa.Uuid()),
        sa.column("release_version_id", sa.Uuid()),
        sa.column("revision_no", sa.Integer()),
        sa.column("watermark_asset_id", sa.Uuid()),
        sa.column("request_sha256", sa.String()),
        sa.column("created_by_user_id", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    heads = sa.table(
        "x_teaser_revision_heads",
        sa.column("id", sa.Uuid()),
        sa.column("review_task_id", sa.Uuid()),
        sa.column("release_version_id", sa.Uuid()),
        sa.column("active_revision_id", sa.Uuid()),
        sa.column("pending_revision_id", sa.Uuid()),
        sa.column("lock_version", sa.Integer()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    members = sa.table(
        "x_teaser_revision_members",
        sa.column("id", sa.Uuid()),
        sa.column("revision_id", sa.Uuid()),
        sa.column("review_task_id", sa.Uuid()),
        sa.column("release_version_id", sa.Uuid()),
        sa.column("release_selection_id", sa.Uuid()),
        sa.column("source_asset_id", sa.Uuid()),
        sa.column("display_order", sa.Integer()),
        sa.column("watermark_position", sa.String()),
        sa.column("derivative_recipe_id", sa.Uuid()),
        sa.column("derivative_job_id", sa.Uuid()),
        sa.column("derivative_output_id", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    for review_task_id, task_selections in selected_by_task.items():
        chosen = [candidate_by_selection.get(row["id"]) for row in task_selections]
        if any(row is None for row in chosen):
            continue
        complete = [row for row in chosen if row is not None]
        watermark_ids = {row["watermark_asset_id"] for row in complete}
        if len(watermark_ids) != 1:
            continue
        positions: dict[UUID, str] = {}
        valid = True
        for selection, candidate in zip(task_selections, complete, strict=True):
            position = _watermark_position(candidate["configuration"])
            if position is None:
                valid = False
                break
            positions[selection["asset_id"]] = position
        if not valid:
            continue
        release_version_id = task_selections[0]["release_version_id"]
        revision_id = uuid5(_REVISION_NAMESPACE, f"{review_task_id}:legacy-active:1")
        head_id = uuid5(_HEAD_NAMESPACE, str(review_task_id))
        recorded_times = [row["recorded_at"] for row in complete if row["recorded_at"]]
        created_at = max(recorded_times) if recorded_times else datetime.now(UTC)
        request_sha256 = _canonical_sha256(
            {
                "schema": "x-teaser-revision-request/v1",
                "review_task_id": str(review_task_id),
                "release_version_id": str(release_version_id),
                "watermark_asset_id": str(next(iter(watermark_ids))),
                "placements": {
                    str(asset_id): positions[asset_id] for asset_id in sorted(positions, key=str)
                },
            }
        )
        bind.execute(
            revisions.insert(),
            {
                "id": revision_id,
                "review_task_id": review_task_id,
                "release_version_id": release_version_id,
                "revision_no": 1,
                "watermark_asset_id": next(iter(watermark_ids)),
                "request_sha256": request_sha256,
                "created_by_user_id": complete[0]["created_by_user_id"],
                "created_at": created_at,
            },
        )
        bind.execute(
            members.insert(),
            [
                {
                    "id": uuid5(_MEMBER_NAMESPACE, f"{revision_id}:{selection['id']}"),
                    "revision_id": revision_id,
                    "review_task_id": review_task_id,
                    "release_version_id": release_version_id,
                    "release_selection_id": selection["id"],
                    "source_asset_id": selection["asset_id"],
                    "display_order": selection["display_order"],
                    "watermark_position": positions[selection["asset_id"]],
                    "derivative_recipe_id": candidate["derivative_recipe_id"],
                    "derivative_job_id": None,
                    "derivative_output_id": candidate["output_id"],
                    "created_at": created_at,
                }
                for selection, candidate in zip(task_selections, complete, strict=True)
            ],
        )
        bind.execute(
            heads.insert(),
            {
                "id": head_id,
                "review_task_id": review_task_id,
                "release_version_id": release_version_id,
                "active_revision_id": revision_id,
                "pending_revision_id": None,
                "lock_version": 1,
                "updated_at": created_at,
            },
        )


def _watermark_position(configuration: object) -> str | None:
    if isinstance(configuration, str):
        try:
            configuration = json.loads(configuration)
        except json.JSONDecodeError:
            return None
    if not isinstance(configuration, Mapping):
        return None
    watermark = configuration.get("watermark")
    if not isinstance(watermark, Mapping):
        return None
    position = watermark.get("position")
    if position not in {"top_left", "top_right", "bottom_left", "bottom_right"}:
        return None
    return str(position)


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _drop_sqlite_job_triggers() -> None:
    for trigger in _JOB_TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")


def _drop_postgresql_job_triggers() -> None:
    for trigger in (
        "derivative_jobs_guard_insert",
        "derivative_jobs_guard_mutation",
        "derivative_jobs_promote_release_after_success",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON derivative_jobs")


def _create_postgresql_job_triggers() -> None:
    for statement in (
        "CREATE TRIGGER derivative_jobs_guard_insert BEFORE INSERT ON "
        "derivative_jobs FOR EACH ROW EXECUTE FUNCTION "
        "gen_automation_guard_derivative_job_insert()",
        "CREATE TRIGGER derivative_jobs_guard_mutation BEFORE UPDATE OR DELETE ON "
        "derivative_jobs FOR EACH ROW EXECUTE FUNCTION "
        "gen_automation_guard_derivative_job_mutation()",
        "CREATE TRIGGER derivative_jobs_promote_release_after_success AFTER UPDATE ON "
        "derivative_jobs FOR EACH ROW EXECUTE FUNCTION "
        "gen_automation_promote_rendered_release()",
    ):
        op.execute(statement)


def _drop_sqlite_dependent_job_triggers(bind: sa.Connection) -> tuple[str, ...]:
    """Temporarily remove triggers that SQLite cannot retarget during a table rebuild."""

    rows = bind.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' "
            "AND tbl_name NOT IN ('derivative_jobs', 'x_teaser_revision_members', "
            "'x_teaser_revisions', 'x_teaser_revision_heads') "
            "AND sql IS NOT NULL AND instr(lower(sql), 'derivative_jobs') > 0 "
            "ORDER BY name"
        )
    ).all()
    statements: list[str] = []
    for name, statement in rows:
        statements.append(str(statement))
        escaped_name = str(name).replace('"', '""')
        op.execute(f'DROP TRIGGER IF EXISTS "{escaped_name}"')
    return tuple(statements)


def _create_sqlite_revision_triggers() -> None:
    statements = (
        "CREATE TRIGGER x_teaser_revisions_reject_update BEFORE UPDATE ON "
        "x_teaser_revisions BEGIN SELECT RAISE(ABORT, 'X teaser revisions are append-only'); END",
        "CREATE TRIGGER x_teaser_revisions_reject_delete BEFORE DELETE ON "
        "x_teaser_revisions BEGIN SELECT RAISE(ABORT, 'X teaser revisions are append-only'); END",
        """CREATE TRIGGER x_teaser_revision_members_guard_insert BEFORE INSERT ON
x_teaser_revision_members BEGIN SELECT CASE WHEN NOT EXISTS (SELECT 1
FROM x_teaser_revisions AS revision JOIN release_selections AS selection
ON selection.id = NEW.release_selection_id JOIN derivative_recipes AS recipe
ON recipe.id = NEW.derivative_recipe_id WHERE revision.id = NEW.revision_id
AND revision.review_task_id = NEW.review_task_id
AND revision.release_version_id = NEW.release_version_id
AND selection.review_task_id = NEW.review_task_id
AND selection.release_version_id = NEW.release_version_id
AND selection.asset_id = NEW.source_asset_id
AND recipe.release_version_id = NEW.release_version_id
AND recipe.watermark_asset_id = revision.watermark_asset_id
AND json_extract(recipe.configuration, '$.watermark.position') = NEW.watermark_position
AND ((NEW.derivative_job_id IS NOT NULL AND EXISTS (SELECT 1 FROM derivative_jobs AS job
WHERE job.id = NEW.derivative_job_id AND job.release_selection_id = NEW.release_selection_id
AND job.derivative_recipe_id = NEW.derivative_recipe_id
AND job.x_teaser_revision_id = NEW.revision_id)) OR
(NEW.derivative_output_id IS NOT NULL AND EXISTS (SELECT 1 FROM derivative_outputs AS output
WHERE output.id = NEW.derivative_output_id
AND output.release_selection_id = NEW.release_selection_id
AND output.derivative_recipe_id = NEW.derivative_recipe_id
AND output.target = 'x_teaser')))
) THEN RAISE(ABORT, 'X teaser revision member is invalid') END; END""",
        "CREATE TRIGGER x_teaser_revision_members_reject_update BEFORE UPDATE ON "
        "x_teaser_revision_members BEGIN SELECT RAISE(ABORT, "
        "'X teaser revision members are append-only'); END",
        "CREATE TRIGGER x_teaser_revision_members_reject_delete BEFORE DELETE ON "
        "x_teaser_revision_members BEGIN SELECT RAISE(ABORT, "
        "'X teaser revision members are append-only'); END",
        """CREATE TRIGGER x_teaser_revision_heads_guard_update BEFORE UPDATE ON
x_teaser_revision_heads BEGIN SELECT CASE WHEN OLD.id IS NOT NEW.id
OR OLD.review_task_id IS NOT NEW.review_task_id
OR OLD.release_version_id IS NOT NEW.release_version_id
OR NEW.lock_version <> OLD.lock_version + 1 OR NOT (
(OLD.pending_revision_id IS NULL AND NEW.pending_revision_id IS NOT NULL
AND NEW.active_revision_id IS OLD.active_revision_id) OR
(OLD.pending_revision_id IS NOT NULL AND NEW.pending_revision_id IS NULL AND
(NEW.active_revision_id IS OLD.pending_revision_id
OR NEW.active_revision_id IS OLD.active_revision_id)))
THEN RAISE(ABORT, 'X teaser revision head transition is invalid') END; END""",
        "CREATE TRIGGER x_teaser_revision_heads_reject_delete BEFORE DELETE ON "
        "x_teaser_revision_heads BEGIN SELECT RAISE(ABORT, "
        "'X teaser revision head cannot be deleted'); END",
    )
    for statement in statements:
        op.execute(statement)


def _create_postgresql_revision_guards() -> None:
    statements = (
        """CREATE OR REPLACE FUNCTION gen_automation_guard_x_teaser_revision_mutation()
RETURNS trigger AS $$ BEGIN IF TG_OP <> 'INSERT' THEN
RAISE EXCEPTION 'X teaser revisions are append-only'; END IF;
RETURN NEW; END; $$ LANGUAGE plpgsql""",
        """CREATE TRIGGER x_teaser_revisions_guard BEFORE UPDATE OR DELETE ON
x_teaser_revisions FOR EACH ROW EXECUTE FUNCTION
gen_automation_guard_x_teaser_revision_mutation()""",
        """CREATE OR REPLACE FUNCTION gen_automation_guard_x_teaser_revision_member_mutation()
RETURNS trigger AS $$ BEGIN IF TG_OP <> 'INSERT' THEN
RAISE EXCEPTION 'X teaser revision members are append-only'; END IF;
IF NOT EXISTS (SELECT 1 FROM x_teaser_revisions AS revision
JOIN release_selections AS selection ON selection.id = NEW.release_selection_id
JOIN derivative_recipes AS recipe ON recipe.id = NEW.derivative_recipe_id
WHERE revision.id = NEW.revision_id AND revision.review_task_id = NEW.review_task_id
AND revision.release_version_id = NEW.release_version_id
AND selection.review_task_id = NEW.review_task_id
AND selection.release_version_id = NEW.release_version_id
AND selection.asset_id = NEW.source_asset_id
AND recipe.release_version_id = NEW.release_version_id
AND recipe.watermark_asset_id = revision.watermark_asset_id
AND recipe.configuration #>> '{watermark,position}' = NEW.watermark_position
AND ((NEW.derivative_job_id IS NOT NULL AND EXISTS (SELECT 1 FROM derivative_jobs AS job
WHERE job.id = NEW.derivative_job_id AND job.release_selection_id = NEW.release_selection_id
AND job.derivative_recipe_id = NEW.derivative_recipe_id
AND job.x_teaser_revision_id = NEW.revision_id)) OR
(NEW.derivative_output_id IS NOT NULL AND EXISTS (SELECT 1 FROM derivative_outputs AS output
WHERE output.id = NEW.derivative_output_id
AND output.release_selection_id = NEW.release_selection_id
AND output.derivative_recipe_id = NEW.derivative_recipe_id
AND output.target = 'x_teaser')))) THEN
RAISE EXCEPTION 'X teaser revision member is invalid'; END IF;
RETURN NEW; END; $$ LANGUAGE plpgsql""",
        """CREATE TRIGGER x_teaser_revision_members_guard BEFORE INSERT OR UPDATE OR DELETE ON
x_teaser_revision_members FOR EACH ROW EXECUTE FUNCTION
gen_automation_guard_x_teaser_revision_member_mutation()""",
        """CREATE OR REPLACE FUNCTION gen_automation_guard_x_teaser_revision_head_mutation()
RETURNS trigger AS $$ BEGIN IF TG_OP = 'DELETE' THEN
RAISE EXCEPTION 'X teaser revision head cannot be deleted'; END IF;
IF TG_OP = 'UPDATE' AND (OLD.id IS DISTINCT FROM NEW.id
OR OLD.review_task_id IS DISTINCT FROM NEW.review_task_id
OR OLD.release_version_id IS DISTINCT FROM NEW.release_version_id
OR NEW.lock_version <> OLD.lock_version + 1 OR NOT (
(OLD.pending_revision_id IS NULL AND NEW.pending_revision_id IS NOT NULL
AND NEW.active_revision_id IS NOT DISTINCT FROM OLD.active_revision_id) OR
(OLD.pending_revision_id IS NOT NULL AND NEW.pending_revision_id IS NULL AND
(NEW.active_revision_id IS NOT DISTINCT FROM OLD.pending_revision_id
OR NEW.active_revision_id IS NOT DISTINCT FROM OLD.active_revision_id)))) THEN
RAISE EXCEPTION 'X teaser revision head transition is invalid'; END IF;
RETURN NEW; END; $$ LANGUAGE plpgsql""",
        """CREATE TRIGGER x_teaser_revision_heads_guard BEFORE UPDATE OR DELETE ON
x_teaser_revision_heads FOR EACH ROW EXECUTE FUNCTION
gen_automation_guard_x_teaser_revision_head_mutation()""",
    )
    for statement in statements:
        op.execute(statement)


def _drop_revision_triggers(dialect_name: str) -> None:
    if dialect_name == "sqlite":
        for trigger in (
            "x_teaser_revisions_reject_update",
            "x_teaser_revisions_reject_delete",
            "x_teaser_revision_members_guard_insert",
            "x_teaser_revision_members_reject_update",
            "x_teaser_revision_members_reject_delete",
            "x_teaser_revision_heads_guard_update",
            "x_teaser_revision_heads_reject_delete",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        return
    for trigger, table in (
        ("x_teaser_revisions_guard", "x_teaser_revisions"),
        ("x_teaser_revision_members_guard", "x_teaser_revision_members"),
        ("x_teaser_revision_heads_guard", "x_teaser_revision_heads"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    for function in (
        "gen_automation_guard_x_teaser_revision_mutation",
        "gen_automation_guard_x_teaser_revision_member_mutation",
        "gen_automation_guard_x_teaser_revision_head_mutation",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")


def _legacy_postgresql_job_functions() -> tuple[str, ...]:
    insert = _POSTGRES_JOB_FUNCTIONS[0].replace(
        "AND ((NEW.gates_release AND release.phase = 'rendering') OR\n"
        "(NOT NEW.gates_release AND release.phase IN\n"
        "('rendering', 'ready_to_publish', 'publishing', 'published')\n"
        "AND EXISTS (SELECT 1 FROM x_teaser_revision_heads AS head\n"
        "WHERE head.review_task_id = selection.review_task_id\n"
        "AND head.release_version_id = NEW.release_version_id\n"
        "AND (head.active_revision_id IS NOT NULL OR release.phase <> 'rendering')\n"
        "AND head.pending_revision_id = NEW.x_teaser_revision_id)))",
        "AND release.phase = 'rendering'",
    )
    mutation = (
        _POSTGRES_JOB_FUNCTIONS[1]
        .replace("OR OLD.x_teaser_revision_id IS DISTINCT FROM NEW.x_teaser_revision_id\n", "")
        .replace("OR OLD.gates_release IS DISTINCT FROM NEW.gates_release\n", "")
        .replace(
            "IF NEW.state = 'succeeded' AND NOT EXISTS (\n"
            "SELECT 1 FROM release_versions AS version\n"
            "JOIN releases AS release ON release.id = version.release_id\n"
            "JOIN release_selections AS selection ON selection.id = OLD.release_selection_id\n"
            "WHERE version.id = OLD.release_version_id\n"
            "AND release.current_version_no = version.version_no\n"
            "AND ((NEW.gates_release AND release.phase = 'rendering') OR\n"
            "(NOT NEW.gates_release AND release.phase IN\n"
            "('rendering', 'ready_to_publish', 'publishing', 'published')\n"
            "AND EXISTS (SELECT 1 FROM x_teaser_revision_heads AS head\n"
            "WHERE head.review_task_id = selection.review_task_id\n"
            "AND head.release_version_id = NEW.release_version_id\n"
            "AND (head.active_revision_id IS NOT NULL OR release.phase <> 'rendering')\n"
            "AND head.pending_revision_id = NEW.x_teaser_revision_id)))) THEN\n"
            "RAISE EXCEPTION 'derivative job release phase is invalid'; END IF;",
            "IF NEW.state = 'succeeded' AND NOT EXISTS (\n"
            "SELECT 1 FROM release_versions AS version\n"
            "JOIN releases AS release ON release.id = version.release_id\n"
            "WHERE version.id = OLD.release_version_id\n"
            "AND release.current_version_no = version.version_no\n"
            "AND release.phase = 'rendering') THEN\n"
            "RAISE EXCEPTION 'derivative job release phase is invalid'; END IF;",
        )
    )
    promote = """
CREATE OR REPLACE FUNCTION gen_automation_promote_rendered_release()
RETURNS trigger AS $$ DECLARE changed_count integer; BEGIN
IF OLD.state <> 'succeeded' AND NEW.state = 'succeeded'
AND NOT EXISTS (SELECT 1 FROM derivative_jobs AS pending
WHERE pending.release_version_id = NEW.release_version_id
AND pending.state <> 'succeeded') THEN
UPDATE releases SET phase = 'ready_to_publish', lock_version = lock_version + 1
WHERE phase = 'rendering' AND id = (
SELECT version.release_id FROM release_versions AS version
WHERE version.id = NEW.release_version_id
AND version.version_no = releases.current_version_no);
GET DIAGNOSTICS changed_count = ROW_COUNT;
IF changed_count <> 1 THEN
RAISE EXCEPTION 'release readiness compare-and-swap failed'; END IF;
END IF; RETURN NEW; END; $$ LANGUAGE plpgsql
"""
    return insert, mutation, promote
