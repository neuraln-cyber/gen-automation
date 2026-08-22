"""Allow an open review target to expand to its frozen ranking size.

Revision ID: 20260822_0039
Revises: 20260818_0038
Create Date: 2026-08-22
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260822_0039"
down_revision: str | None = "20260818_0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SQLITE_EXPANDABLE_GUARD = """
CREATE TRIGGER review_tasks_guard_update
BEFORE UPDATE ON review_tasks
BEGIN
    SELECT CASE WHEN
        OLD.state <> 'open'
        OR NEW.lock_version <> OLD.lock_version + 1
        OR OLD.id IS NOT NEW.id
        OR OLD.release_version_id IS NOT NEW.release_version_id
        OR OLD.release_version_no IS NOT NEW.release_version_no
        OR OLD.release_specification_sha256 IS NOT NEW.release_specification_sha256
        OR OLD.scoring_run_id IS NOT NEW.scoring_run_id
        OR OLD.scoring_config_sha256 IS NOT NEW.scoring_config_sha256
        OR OLD.scoring_input_manifest_sha256 IS NOT NEW.scoring_input_manifest_sha256
        OR OLD.ranking_manifest_sha256 IS NOT NEW.ranking_manifest_sha256
        OR OLD.ranked_asset_count IS NOT NEW.ranked_asset_count
        OR OLD.created_by_user_id IS NOT NEW.created_by_user_id
        OR OLD.created_at IS NOT NEW.created_at
    THEN RAISE(ABORT, 'review task identity is immutable') END;
    SELECT CASE WHEN
        OLD.desired_accepted_count IS NOT NEW.desired_accepted_count
        AND NEW.state IS 'open'
        AND (
            NEW.desired_accepted_count IS NULL
            OR NEW.desired_accepted_count < OLD.desired_accepted_count
            OR NEW.desired_accepted_count > OLD.ranked_asset_count
        )
    THEN RAISE(ABORT, 'open review target expansion is invalid') END;
    SELECT CASE WHEN
        OLD.desired_accepted_count IS NOT NEW.desired_accepted_count
        AND NEW.state NOT IN ('open', 'completed')
    THEN RAISE(ABORT, 'review task acceptance target is immutable') END;
    SELECT CASE WHEN NEW.state = 'completed' AND (
        NEW.desired_accepted_count IS NULL
        OR NEW.desired_accepted_count <= 0
        OR NEW.desired_accepted_count > OLD.desired_accepted_count
    ) THEN RAISE(ABORT, 'review task acceptance target shrink is invalid') END;
    SELECT CASE WHEN NEW.state = 'completed' AND (
        SELECT count(*)
        FROM review_decisions AS decision
        WHERE decision.review_task_id = OLD.id
          AND decision.decision = 'accept'
          AND NOT EXISTS (
              SELECT 1
              FROM review_decisions AS newer
              WHERE newer.review_task_id = decision.review_task_id
                AND newer.asset_id = decision.asset_id
                AND newer.revision > decision.revision
          )
    ) <> NEW.desired_accepted_count
    THEN RAISE(ABORT, 'review task acceptance target is not satisfied') END;
END
"""


_SQLITE_SHRINK_ONLY_GUARD = _SQLITE_EXPANDABLE_GUARD.replace(
    """    SELECT CASE WHEN
        OLD.desired_accepted_count IS NOT NEW.desired_accepted_count
        AND NEW.state IS 'open'
        AND (
            NEW.desired_accepted_count IS NULL
            OR NEW.desired_accepted_count < OLD.desired_accepted_count
            OR NEW.desired_accepted_count > OLD.ranked_asset_count
        )
    THEN RAISE(ABORT, 'open review target expansion is invalid') END;
    SELECT CASE WHEN
        OLD.desired_accepted_count IS NOT NEW.desired_accepted_count
        AND NEW.state NOT IN ('open', 'completed')
    THEN RAISE(ABORT, 'review task acceptance target is immutable') END;
""",
    """    SELECT CASE WHEN
        OLD.desired_accepted_count IS NOT NEW.desired_accepted_count
        AND NEW.state IS NOT 'completed'
    THEN RAISE(
        ABORT,
        'review task acceptance target may shrink only on completion'
    ) END;
""",
)


_POSTGRESQL_EXPANDABLE_GUARD = """
CREATE OR REPLACE FUNCTION gen_automation_guard_review_task_mutation()
RETURNS trigger AS $$
DECLARE
    accepted_count integer;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'review tasks cannot be deleted';
    END IF;
    IF OLD.state <> 'open'
       OR NEW.lock_version <> OLD.lock_version + 1
       OR OLD.id IS DISTINCT FROM NEW.id
       OR OLD.release_version_id IS DISTINCT FROM NEW.release_version_id
       OR OLD.release_version_no IS DISTINCT FROM NEW.release_version_no
       OR OLD.release_specification_sha256 IS DISTINCT FROM NEW.release_specification_sha256
       OR OLD.scoring_run_id IS DISTINCT FROM NEW.scoring_run_id
       OR OLD.scoring_config_sha256 IS DISTINCT FROM NEW.scoring_config_sha256
       OR OLD.scoring_input_manifest_sha256 IS DISTINCT FROM NEW.scoring_input_manifest_sha256
       OR OLD.ranking_manifest_sha256 IS DISTINCT FROM NEW.ranking_manifest_sha256
       OR OLD.ranked_asset_count IS DISTINCT FROM NEW.ranked_asset_count
       OR OLD.created_by_user_id IS DISTINCT FROM NEW.created_by_user_id
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'review task identity is immutable';
    END IF;
    IF OLD.desired_accepted_count IS DISTINCT FROM NEW.desired_accepted_count
       AND NEW.state = 'open' AND (
           NEW.desired_accepted_count IS NULL
           OR NEW.desired_accepted_count < OLD.desired_accepted_count
           OR NEW.desired_accepted_count > OLD.ranked_asset_count
       ) THEN
        RAISE EXCEPTION 'open review target expansion is invalid';
    END IF;
    IF OLD.desired_accepted_count IS DISTINCT FROM NEW.desired_accepted_count
       AND NEW.state NOT IN ('open', 'completed') THEN
        RAISE EXCEPTION 'review task acceptance target is immutable';
    END IF;
    IF NEW.state = 'completed' AND (
        NEW.desired_accepted_count IS NULL
        OR NEW.desired_accepted_count <= 0
        OR NEW.desired_accepted_count > OLD.desired_accepted_count
    ) THEN
        RAISE EXCEPTION 'review task acceptance target shrink is invalid';
    END IF;
    IF NEW.state = 'completed' THEN
        SELECT count(*) INTO accepted_count
        FROM review_decisions AS decision
        WHERE decision.review_task_id = OLD.id
          AND decision.decision = 'accept'
          AND NOT EXISTS (
              SELECT 1
              FROM review_decisions AS newer
              WHERE newer.review_task_id = decision.review_task_id
                AND newer.asset_id = decision.asset_id
                AND newer.revision > decision.revision
          );
        IF accepted_count <> NEW.desired_accepted_count THEN
            RAISE EXCEPTION 'review task acceptance target is not satisfied';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


_POSTGRESQL_SHRINK_ONLY_GUARD = _POSTGRESQL_EXPANDABLE_GUARD.replace(
    """    IF OLD.desired_accepted_count IS DISTINCT FROM NEW.desired_accepted_count
       AND NEW.state = 'open' AND (
           NEW.desired_accepted_count IS NULL
           OR NEW.desired_accepted_count < OLD.desired_accepted_count
           OR NEW.desired_accepted_count > OLD.ranked_asset_count
       ) THEN
        RAISE EXCEPTION 'open review target expansion is invalid';
    END IF;
    IF OLD.desired_accepted_count IS DISTINCT FROM NEW.desired_accepted_count
       AND NEW.state NOT IN ('open', 'completed') THEN
        RAISE EXCEPTION 'review task acceptance target is immutable';
    END IF;
""",
    """    IF OLD.desired_accepted_count IS DISTINCT FROM NEW.desired_accepted_count
       AND NEW.state <> 'completed' THEN
        RAISE EXCEPTION
            'review task acceptance target may shrink only on completion';
    END IF;
""",
)


def _replace_guard(*, expandable: bool) -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS review_tasks_guard_update")
        op.execute(_SQLITE_EXPANDABLE_GUARD if expandable else _SQLITE_SHRINK_ONLY_GUARD)
        return
    if dialect == "postgresql":
        op.execute(_POSTGRESQL_EXPANDABLE_GUARD if expandable else _POSTGRESQL_SHRINK_ONLY_GUARD)
        return
    raise NotImplementedError(f"unsupported database dialect: {dialect}")


def upgrade() -> None:
    _replace_guard(expandable=True)


def downgrade() -> None:
    _replace_guard(expandable=False)
