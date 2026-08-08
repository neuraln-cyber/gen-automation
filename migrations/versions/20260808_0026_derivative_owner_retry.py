"""Permit a bounded owner retry of failed clean derivative jobs.

Revision ID: 20260808_0026
Revises: 20260808_0025
Create Date: 2026-08-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260808_0026"
down_revision: str | None = "20260808_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SQLITE_GUARD = """
CREATE TRIGGER derivative_jobs_guard_update
BEFORE UPDATE ON derivative_jobs BEGIN
SELECT CASE WHEN OLD.state IN ('succeeded', 'cancelled')
OR NEW.lock_version <> OLD.lock_version + 1
OR OLD.id IS NOT NEW.id
OR OLD.release_selection_id IS NOT NEW.release_selection_id
OR OLD.derivative_recipe_id IS NOT NEW.derivative_recipe_id
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
WHERE version.id = OLD.release_version_id
AND release.current_version_no = version.version_no
AND release.phase = 'rendering')
THEN RAISE(ABORT, 'derivative job release phase is invalid') END;
END
"""

_SQLITE_LEGACY_GUARD = (
    _SQLITE_GUARD.replace(
        "OLD.state IN ('succeeded', 'cancelled')",
        "OLD.state IN ('succeeded', 'failed', 'cancelled')",
    )
    .replace(
        "OR (OLD.max_attempts IS NOT NEW.max_attempts AND NOT (\n"
        "OLD.state = 'failed' AND NEW.state = 'retry_wait'\n"
        "AND NEW.max_attempts > OLD.max_attempts\n"
        "AND NEW.max_attempts <= 10\n"
        "AND NEW.max_attempts >= OLD.attempt_count + 1))",
        "OR OLD.max_attempts IS NOT NEW.max_attempts",
    )
    .replace(
        "\nOR (OLD.state = 'failed' AND NEW.state = 'retry_wait')",
        "",
    )
    .replace(
        "SELECT CASE WHEN OLD.state = 'failed' AND (\n"
        "NEW.state <> 'retry_wait'\n"
        "OR OLD.last_error_code IS 'output_object_conflict'\n"
        "OR NEW.completed_at IS NOT NULL\n"
        "OR NEW.last_error_code IS NOT NULL OR NEW.last_error_detail IS NOT NULL\n"
        "OR NEW.lease_owner IS NOT NULL OR NEW.lease_expires_at IS NOT NULL\n"
        "OR NEW.retry_at IS NULL\n"
        "OR NEW.max_attempts <= OLD.max_attempts\n"
        "OR NEW.max_attempts > 10\n"
        "OR NEW.max_attempts < OLD.attempt_count + 1\n"
        "OR NOT EXISTS (SELECT 1 FROM derivative_recipes AS retry_recipe\n"
        "JOIN release_versions AS retry_version ON retry_version.id = OLD.release_version_id\n"
        "JOIN releases AS retry_release ON retry_release.id = retry_version.release_id\n"
        "WHERE retry_recipe.id = OLD.derivative_recipe_id\n"
        "AND retry_recipe.release_version_id = OLD.release_version_id\n"
        "AND json_array_length(retry_recipe.output_targets) = 1\n"
        "AND json_extract(retry_recipe.output_targets, '$[0]') = 'full'\n"
        "AND retry_release.current_version_no = retry_version.version_no\n"
        "AND retry_release.phase = 'rendering'))\n"
        "THEN RAISE(ABORT, 'failed derivative job rearm is invalid') END;\n",
        "",
    )
)

_POSTGRESQL_GUARD = """
CREATE OR REPLACE FUNCTION gen_automation_guard_derivative_job_mutation()
RETURNS trigger AS $$ BEGIN
IF TG_OP = 'DELETE' THEN
RAISE EXCEPTION 'derivative jobs cannot be deleted'; END IF;
IF OLD.state IN ('succeeded', 'cancelled')
OR NEW.lock_version <> OLD.lock_version + 1
OR OLD.id IS DISTINCT FROM NEW.id
OR OLD.release_selection_id IS DISTINCT FROM NEW.release_selection_id
OR OLD.derivative_recipe_id IS DISTINCT FROM NEW.derivative_recipe_id
OR OLD.release_version_id IS DISTINCT FROM NEW.release_version_id
OR OLD.logical_key IS DISTINCT FROM NEW.logical_key
OR OLD.request_payload IS DISTINCT FROM NEW.request_payload
OR OLD.request_sha256 IS DISTINCT FROM NEW.request_sha256
OR OLD.expected_output_count IS DISTINCT FROM NEW.expected_output_count
OR OLD.priority IS DISTINCT FROM NEW.priority
OR (OLD.max_attempts IS DISTINCT FROM NEW.max_attempts AND NOT (
OLD.state = 'failed' AND NEW.state = 'retry_wait'
AND NEW.max_attempts > OLD.max_attempts
AND NEW.max_attempts <= 10
AND NEW.max_attempts >= OLD.attempt_count + 1))
OR OLD.available_at IS DISTINCT FROM NEW.available_at
OR OLD.requested_at IS DISTINCT FROM NEW.requested_at THEN
RAISE EXCEPTION 'derivative job identity is immutable'; END IF;
IF NOT (
(OLD.state IN ('requested', 'retry_wait') AND NEW.state IN ('claimed', 'cancelled'))
OR (OLD.state = 'claimed'
AND NEW.state IN ('claimed', 'processing', 'retry_wait', 'failed', 'cancelled'))
OR (OLD.state = 'processing'
AND NEW.state IN ('claimed', 'retry_wait', 'succeeded', 'failed', 'cancelled'))
OR (OLD.state = 'failed' AND NEW.state = 'retry_wait')
) THEN RAISE EXCEPTION 'derivative job state transition is invalid'; END IF;
IF NEW.state = 'claimed' AND NEW.attempt_count <> OLD.attempt_count + 1 THEN
RAISE EXCEPTION 'derivative job claim attempt is invalid'; END IF;
IF NEW.state <> 'claimed' AND NEW.attempt_count <> OLD.attempt_count THEN
RAISE EXCEPTION 'derivative job attempt count is immutable'; END IF;
IF OLD.state = 'failed' AND (
NEW.state <> 'retry_wait'
OR OLD.last_error_code IS NOT DISTINCT FROM 'output_object_conflict'
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
WHERE version.id = OLD.release_version_id
AND release.current_version_no = version.version_no
AND release.phase = 'rendering') THEN
RAISE EXCEPTION 'derivative job release phase is invalid'; END IF;
RETURN NEW; END; $$ LANGUAGE plpgsql
"""

_POSTGRESQL_LEGACY_GUARD = (
    _POSTGRESQL_GUARD.replace(
        "OLD.state IN ('succeeded', 'cancelled')",
        "OLD.state IN ('succeeded', 'failed', 'cancelled')",
    )
    .replace(
        "OR (OLD.max_attempts IS DISTINCT FROM NEW.max_attempts AND NOT (\n"
        "OLD.state = 'failed' AND NEW.state = 'retry_wait'\n"
        "AND NEW.max_attempts > OLD.max_attempts\n"
        "AND NEW.max_attempts <= 10\n"
        "AND NEW.max_attempts >= OLD.attempt_count + 1))",
        "OR OLD.max_attempts IS DISTINCT FROM NEW.max_attempts",
    )
    .replace(
        "\nOR (OLD.state = 'failed' AND NEW.state = 'retry_wait')",
        "",
    )
    .replace(
        "IF OLD.state = 'failed' AND (\n"
        "NEW.state <> 'retry_wait'\n"
        "OR OLD.last_error_code IS NOT DISTINCT FROM 'output_object_conflict'\n"
        "OR NEW.completed_at IS NOT NULL\n"
        "OR NEW.last_error_code IS NOT NULL OR NEW.last_error_detail IS NOT NULL\n"
        "OR NEW.lease_owner IS NOT NULL OR NEW.lease_expires_at IS NOT NULL\n"
        "OR NEW.retry_at IS NULL\n"
        "OR NEW.max_attempts <= OLD.max_attempts\n"
        "OR NEW.max_attempts > 10\n"
        "OR NEW.max_attempts < OLD.attempt_count + 1\n"
        "OR NOT EXISTS (SELECT 1 FROM derivative_recipes AS retry_recipe\n"
        "JOIN release_versions AS retry_version ON retry_version.id = OLD.release_version_id\n"
        "JOIN releases AS retry_release ON retry_release.id = retry_version.release_id\n"
        "WHERE retry_recipe.id = OLD.derivative_recipe_id\n"
        "AND retry_recipe.release_version_id = OLD.release_version_id\n"
        "AND retry_recipe.output_targets::jsonb = '[\"full\"]'::jsonb\n"
        "AND retry_release.current_version_no = retry_version.version_no\n"
        "AND retry_release.phase = 'rendering')) THEN\n"
        "RAISE EXCEPTION 'failed derivative job rearm is invalid'; END IF;\n",
        "",
    )
)


def _replace_guard(statement: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS derivative_jobs_guard_update")
    op.execute(statement)


def upgrade() -> None:
    bind = op.get_bind()
    _replace_guard(_SQLITE_GUARD if bind.dialect.name == "sqlite" else _POSTGRESQL_GUARD)


def downgrade() -> None:
    bind = op.get_bind()
    _replace_guard(
        _SQLITE_LEGACY_GUARD if bind.dialect.name == "sqlite" else _POSTGRESQL_LEGACY_GUARD
    )
