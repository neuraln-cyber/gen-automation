"""Allow the accepted-image target to shrink atomically on completion.

Revision ID: 20260805_0018
Revises: 20260803_0017
Create Date: 2026-08-05
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260805_0018"
down_revision: str | None = "20260803_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SQLITE_REVIEW_TASK_GUARD = """
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
        OR OLD.scoring_input_manifest_sha256 IS NOT
            NEW.scoring_input_manifest_sha256
        OR OLD.ranking_manifest_sha256 IS NOT NEW.ranking_manifest_sha256
        OR OLD.desired_accepted_count IS NOT NEW.desired_accepted_count
        OR OLD.ranked_asset_count IS NOT NEW.ranked_asset_count
        OR OLD.created_by_user_id IS NOT NEW.created_by_user_id
        OR OLD.created_at IS NOT NEW.created_at
    THEN RAISE(ABORT, 'review task identity is immutable') END;
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
    ) <> OLD.desired_accepted_count
    THEN RAISE(
        ABORT,
        'review task acceptance target is not satisfied'
    ) END;
END
"""


_POSTGRESQL_REVIEW_TASK_GUARD = """
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
       OR OLD.release_specification_sha256 IS DISTINCT FROM
          NEW.release_specification_sha256
       OR OLD.scoring_run_id IS DISTINCT FROM NEW.scoring_run_id
       OR OLD.scoring_config_sha256 IS DISTINCT FROM NEW.scoring_config_sha256
       OR OLD.scoring_input_manifest_sha256 IS DISTINCT FROM
          NEW.scoring_input_manifest_sha256
       OR OLD.ranking_manifest_sha256 IS DISTINCT FROM
          NEW.ranking_manifest_sha256
       OR OLD.desired_accepted_count IS DISTINCT FROM
          NEW.desired_accepted_count
       OR OLD.ranked_asset_count IS DISTINCT FROM NEW.ranked_asset_count
       OR OLD.created_by_user_id IS DISTINCT FROM NEW.created_by_user_id
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'review task identity is immutable';
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
        IF accepted_count <> OLD.desired_accepted_count THEN
            RAISE EXCEPTION
                'review task acceptance target is not satisfied';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


_SQLITE_SELECTION_COMPLETION_GUARD = """
CREATE TRIGGER review_tasks_validate_selection_completion
BEFORE UPDATE ON review_tasks
WHEN OLD.state = 'open' AND NEW.state = 'completed'
BEGIN
    SELECT CASE WHEN
        (
            SELECT count(*)
            FROM release_selections
            WHERE review_task_id = OLD.id
        ) <> OLD.desired_accepted_count
        OR (
            SELECT min(display_order)
            FROM release_selections
            WHERE review_task_id = OLD.id
        ) <> 1
        OR (
            SELECT max(display_order)
            FROM release_selections
            WHERE review_task_id = OLD.id
        ) <> OLD.desired_accepted_count
        OR NOT EXISTS (
            SELECT 1
            FROM release_versions AS current_version
            JOIN releases AS current_release
              ON current_release.id = current_version.release_id
            WHERE current_version.id = OLD.release_version_id
              AND current_release.current_version_no = current_version.version_no
              AND current_release.phase = 'reviewing'
        )
        OR EXISTS (
            SELECT 1
            FROM release_selections AS selection
            JOIN review_decisions AS decision
              ON decision.id = selection.review_decision_id
            JOIN asset_rankings AS ranking
              ON ranking.scoring_run_id = selection.scoring_run_id
             AND ranking.asset_id = selection.asset_id
            JOIN release_versions AS version
              ON version.id = selection.release_version_id
            JOIN assets AS asset ON asset.id = selection.asset_id
            WHERE selection.review_task_id = OLD.id
              AND (
                  selection.scoring_run_id IS NOT OLD.scoring_run_id
                  OR selection.release_version_id IS NOT OLD.release_version_id
                  OR selection.ranking_manifest_sha256 IS NOT
                     OLD.ranking_manifest_sha256
                  OR selection.frozen_at IS NOT NEW.completed_at
                  OR decision.review_task_id IS NOT OLD.id
                  OR decision.asset_id IS NOT selection.asset_id
                  OR decision.revision IS NOT selection.decision_revision
                  OR decision.decision <> 'accept'
                  OR EXISTS (
                      SELECT 1
                      FROM review_decisions AS newer
                      WHERE newer.review_task_id = decision.review_task_id
                        AND newer.asset_id = decision.asset_id
                        AND newer.revision > decision.revision
                  )
                  OR ranking.rank IS NOT selection.ranking_rank
                  OR asset.release_id IS NOT version.release_id
                  OR asset.kind <> 'raw_master'
                  OR asset.state <> 'available'
                  OR asset.storage_backend IS NOT
                     selection.source_storage_backend
                  OR asset.storage_bucket IS NOT selection.source_storage_bucket
                  OR asset.object_key IS NOT selection.source_object_key
                  OR asset.object_version_id IS NOT
                     selection.source_object_version_id
                  OR asset.sha256 IS NOT selection.source_sha256
                  OR asset.content_type IS NOT selection.source_content_type
                  OR asset.image_format IS NOT selection.source_image_format
                  OR asset.width IS NOT selection.source_width
                  OR asset.height IS NOT selection.source_height
                  OR asset.byte_size IS NOT selection.source_byte_size
                  OR asset.available_at IS NOT selection.source_available_at
              )
        )
        OR EXISTS (
            SELECT 1
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
              AND NOT EXISTS (
                  SELECT 1
                  FROM release_selections AS selection
                  WHERE selection.review_task_id = OLD.id
                    AND selection.review_decision_id = decision.id
              )
        )
        OR EXISTS (
            SELECT 1
            FROM release_selections AS earlier
            JOIN release_selections AS later
              ON later.review_task_id = earlier.review_task_id
            WHERE earlier.review_task_id = OLD.id
              AND earlier.ranking_rank < later.ranking_rank
              AND earlier.display_order > later.display_order
        )
    THEN RAISE(
        ABORT,
        'review completion selection snapshot is invalid'
    ) END;
END
"""


_POSTGRESQL_SELECTION_COMPLETION_GUARD = """
CREATE OR REPLACE FUNCTION gen_automation_validate_selection_completion()
RETURNS trigger AS $$
BEGIN
    IF OLD.state = 'open' AND NEW.state = 'completed' AND (
        (
            SELECT count(*)
            FROM release_selections
            WHERE review_task_id = OLD.id
        ) <> OLD.desired_accepted_count
        OR (
            SELECT min(display_order)
            FROM release_selections
            WHERE review_task_id = OLD.id
        ) <> 1
        OR (
            SELECT max(display_order)
            FROM release_selections
            WHERE review_task_id = OLD.id
        ) <> OLD.desired_accepted_count
        OR NOT EXISTS (
            SELECT 1
            FROM release_versions AS current_version
            JOIN releases AS current_release
              ON current_release.id = current_version.release_id
            WHERE current_version.id = OLD.release_version_id
              AND current_release.current_version_no = current_version.version_no
              AND current_release.phase = 'reviewing'
        )
        OR EXISTS (
            SELECT 1
            FROM release_selections AS selection
            JOIN review_decisions AS decision
              ON decision.id = selection.review_decision_id
            JOIN asset_rankings AS ranking
              ON ranking.scoring_run_id = selection.scoring_run_id
             AND ranking.asset_id = selection.asset_id
            JOIN release_versions AS version
              ON version.id = selection.release_version_id
            JOIN assets AS asset ON asset.id = selection.asset_id
            WHERE selection.review_task_id = OLD.id
              AND (
                  selection.scoring_run_id IS DISTINCT FROM OLD.scoring_run_id
                  OR selection.release_version_id IS DISTINCT FROM
                     OLD.release_version_id
                  OR selection.ranking_manifest_sha256 IS DISTINCT FROM
                     OLD.ranking_manifest_sha256
                  OR selection.frozen_at IS DISTINCT FROM NEW.completed_at
                  OR decision.review_task_id IS DISTINCT FROM OLD.id
                  OR decision.asset_id IS DISTINCT FROM selection.asset_id
                  OR decision.revision IS DISTINCT FROM
                     selection.decision_revision
                  OR decision.decision <> 'accept'
                  OR EXISTS (
                      SELECT 1
                      FROM review_decisions AS newer
                      WHERE newer.review_task_id = decision.review_task_id
                        AND newer.asset_id = decision.asset_id
                        AND newer.revision > decision.revision
                  )
                  OR ranking.rank IS DISTINCT FROM selection.ranking_rank
                  OR asset.release_id IS DISTINCT FROM version.release_id
                  OR asset.kind <> 'raw_master'
                  OR asset.state <> 'available'
                  OR asset.storage_backend IS DISTINCT FROM
                     selection.source_storage_backend
                  OR asset.storage_bucket IS DISTINCT FROM
                     selection.source_storage_bucket
                  OR asset.object_key IS DISTINCT FROM
                     selection.source_object_key
                  OR asset.object_version_id IS DISTINCT FROM
                     selection.source_object_version_id
                  OR asset.sha256 IS DISTINCT FROM selection.source_sha256
                  OR asset.content_type IS DISTINCT FROM
                     selection.source_content_type
                  OR asset.image_format IS DISTINCT FROM
                     selection.source_image_format
                  OR asset.width IS DISTINCT FROM selection.source_width
                  OR asset.height IS DISTINCT FROM selection.source_height
                  OR asset.byte_size IS DISTINCT FROM selection.source_byte_size
                  OR asset.available_at IS DISTINCT FROM
                     selection.source_available_at
              )
        )
        OR EXISTS (
            SELECT 1
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
              AND NOT EXISTS (
                  SELECT 1
                  FROM release_selections AS selection
                  WHERE selection.review_task_id = OLD.id
                    AND selection.review_decision_id = decision.id
              )
        )
        OR EXISTS (
            SELECT 1
            FROM release_selections AS earlier
            JOIN release_selections AS later
              ON later.review_task_id = earlier.review_task_id
            WHERE earlier.review_task_id = OLD.id
              AND earlier.ranking_rank < later.ranking_rank
              AND earlier.display_order > later.display_order
        )
    ) THEN
        RAISE EXCEPTION
            'review completion selection snapshot is invalid';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def _review_task_guard(*, dialect: str, shrink_on_completion: bool) -> str:
    if dialect == "sqlite":
        statement = _SQLITE_REVIEW_TASK_GUARD
        if not shrink_on_completion:
            return statement
        statement = statement.replace(
            "        OR OLD.desired_accepted_count IS NOT NEW.desired_accepted_count\n",
            "",
        )
        target_validation = """
    SELECT CASE WHEN
        OLD.desired_accepted_count IS NOT NEW.desired_accepted_count
        AND NEW.state IS NOT 'completed'
    THEN RAISE(
        ABORT,
        'review task acceptance target may shrink only on completion'
    ) END;
    SELECT CASE WHEN NEW.state = 'completed' AND (
        NEW.desired_accepted_count IS NULL
        OR NEW.desired_accepted_count <= 0
        OR NEW.desired_accepted_count > OLD.desired_accepted_count
    ) THEN RAISE(
        ABORT,
        'review task acceptance target shrink is invalid'
    ) END;
"""
        statement = statement.replace(
            "    SELECT CASE WHEN NEW.state = 'completed' AND (\n",
            target_validation + "    SELECT CASE WHEN NEW.state = 'completed' AND (\n",
            1,
        )
        return statement.replace(
            ") <> OLD.desired_accepted_count\n",
            ") <> NEW.desired_accepted_count\n",
            1,
        )

    statement = _POSTGRESQL_REVIEW_TASK_GUARD
    if not shrink_on_completion:
        return statement
    statement = statement.replace(
        "       OR OLD.desired_accepted_count IS DISTINCT FROM\n"
        "          NEW.desired_accepted_count\n",
        "",
    )
    target_validation = """
    IF OLD.desired_accepted_count IS DISTINCT FROM NEW.desired_accepted_count
       AND NEW.state <> 'completed' THEN
        RAISE EXCEPTION
            'review task acceptance target may shrink only on completion';
    END IF;
    IF NEW.state = 'completed' AND (
        NEW.desired_accepted_count IS NULL
        OR NEW.desired_accepted_count <= 0
        OR NEW.desired_accepted_count > OLD.desired_accepted_count
    ) THEN
        RAISE EXCEPTION
            'review task acceptance target shrink is invalid';
    END IF;
"""
    statement = statement.replace(
        "    IF NEW.state = 'completed' THEN\n",
        target_validation + "    IF NEW.state = 'completed' THEN\n",
        1,
    )
    return statement.replace(
        "accepted_count <> OLD.desired_accepted_count",
        "accepted_count <> NEW.desired_accepted_count",
        1,
    )


def _selection_completion_guard(*, dialect: str, shrink_on_completion: bool) -> str:
    if dialect == "sqlite":
        statement = _SQLITE_SELECTION_COMPLETION_GUARD
    else:
        statement = _POSTGRESQL_SELECTION_COMPLETION_GUARD
    if not shrink_on_completion:
        return statement
    return statement.replace(
        "OLD.desired_accepted_count",
        "NEW.desired_accepted_count",
    )


def _replace_guards(*, shrink_on_completion: bool) -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS review_tasks_guard_update")
        op.execute("DROP TRIGGER IF EXISTS review_tasks_validate_selection_completion")
        op.execute(
            _review_task_guard(
                dialect=dialect,
                shrink_on_completion=shrink_on_completion,
            )
        )
        op.execute(
            _selection_completion_guard(
                dialect=dialect,
                shrink_on_completion=shrink_on_completion,
            )
        )
        return
    if dialect == "postgresql":
        op.execute(
            _review_task_guard(
                dialect=dialect,
                shrink_on_completion=shrink_on_completion,
            )
        )
        op.execute(
            _selection_completion_guard(
                dialect=dialect,
                shrink_on_completion=shrink_on_completion,
            )
        )
        return
    raise NotImplementedError(f"unsupported database dialect: {dialect}")


def upgrade() -> None:
    _replace_guards(shrink_on_completion=True)


def downgrade() -> None:
    _replace_guards(shrink_on_completion=False)
