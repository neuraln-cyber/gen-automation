"""Allow image-only MEGA set delivery completion.

Revision ID: 20260809_0032
Revises: 20260809_0031
Create Date: 2026-08-09

New deliveries retain their source manifest in private storage and the database,
but outward MEGA folders contain only the ordered image files. Historical
deliveries may keep their remote completion-marker handle.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260809_0032"
down_revision: str | None = "20260809_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT_NAME = "ck_mega_set_deliveries_terminal_result_contract"
_IMAGE_ONLY_CONTRACT = (
    "(state = 'succeeded' AND uploaded_item_count = total_item_count "
    "AND total_byte_size IS NOT NULL AND uploaded_byte_size = total_byte_size "
    "AND verified_at IS NOT NULL AND completed_at IS NOT NULL "
    "AND last_error_code IS NULL) OR "
    "(state = 'failed' AND completion_marker_node_handle IS NULL "
    "AND verified_at IS NULL AND completed_at IS NOT NULL "
    "AND last_error_code IS NOT NULL) OR "
    "(state IN ('pending', 'claimed', 'retry_wait') "
    "AND completion_marker_node_handle IS NULL "
    "AND verified_at IS NULL AND completed_at IS NULL)"
)
_LEGACY_CONTRACT = (
    "(state = 'succeeded' AND uploaded_item_count = total_item_count "
    "AND total_byte_size IS NOT NULL AND uploaded_byte_size = total_byte_size "
    "AND completion_marker_node_handle IS NOT NULL "
    "AND verified_at IS NOT NULL AND completed_at IS NOT NULL "
    "AND last_error_code IS NULL) OR "
    "(state = 'failed' AND completion_marker_node_handle IS NULL "
    "AND verified_at IS NULL AND completed_at IS NOT NULL "
    "AND last_error_code IS NOT NULL) OR "
    "(state IN ('pending', 'claimed', 'retry_wait') "
    "AND completion_marker_node_handle IS NULL "
    "AND verified_at IS NULL AND completed_at IS NULL)"
)


def upgrade() -> None:
    _replace_terminal_contract(_IMAGE_ONLY_CONTRACT)


def downgrade() -> None:
    image_only_delivery = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT id FROM mega_set_deliveries "
                "WHERE state = 'succeeded' "
                "AND completion_marker_node_handle IS NULL LIMIT 1"
            )
        )
        .first()
    )
    if image_only_delivery is not None:
        raise RuntimeError(
            "cannot downgrade image-only MEGA folders after a delivery has completed"
        )
    _replace_terminal_contract(_LEGACY_CONTRACT)


def _replace_terminal_contract(expression: str) -> None:
    dialect_name = op.get_bind().dialect.name
    constraint_name = op.f(_CONSTRAINT_NAME)
    if dialect_name == "sqlite":
        _drop_sqlite_delivery_guards()
        with op.batch_alter_table("mega_set_deliveries") as batch_op:
            batch_op.drop_constraint(constraint_name, type_="check")
            batch_op.create_check_constraint(constraint_name, expression)
        _create_sqlite_delivery_guards()
        return
    op.drop_constraint(
        constraint_name,
        "mega_set_deliveries",
        type_="check",
    )
    op.create_check_constraint(
        constraint_name,
        "mega_set_deliveries",
        expression,
    )


def _drop_sqlite_delivery_guards() -> None:
    op.execute(sa.text("DROP TRIGGER IF EXISTS mega_set_deliveries_guard_update"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS mega_set_deliveries_reject_delete"))


def _create_sqlite_delivery_guards() -> None:
    op.execute(
        sa.text(
            "CREATE TRIGGER mega_set_deliveries_guard_update "
            "BEFORE UPDATE ON mega_set_deliveries BEGIN SELECT CASE WHEN "
            "OLD.id IS NOT NEW.id "
            "OR OLD.finished_set_archive_id IS NOT NEW.finished_set_archive_id "
            "OR OLD.remote_root IS NOT NEW.remote_root "
            "OR OLD.remote_folder IS NOT NEW.remote_folder "
            "OR OLD.manifest_sha256 IS NOT NEW.manifest_sha256 "
            "OR OLD.total_item_count IS NOT NEW.total_item_count "
            "OR OLD.created_at IS NOT NEW.created_at "
            "OR (OLD.total_byte_size IS NOT NEW.total_byte_size "
            "AND NOT (OLD.total_byte_size IS NULL AND NEW.total_byte_size IS NOT NULL)) "
            "OR (OLD.source_manifest_json IS NOT NEW.source_manifest_json "
            "AND NOT (OLD.source_manifest_json IS NULL "
            "AND NEW.source_manifest_json IS NOT NULL)) "
            "OR (OLD.planned_at IS NOT NEW.planned_at "
            "AND NOT (OLD.planned_at IS NULL AND NEW.planned_at IS NOT NULL)) "
            "THEN RAISE(ABORT, 'MEGA set delivery identity is immutable') END; END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER mega_set_deliveries_reject_delete "
            "BEFORE DELETE ON mega_set_deliveries BEGIN "
            "SELECT RAISE(ABORT, 'MEGA set deliveries cannot be deleted'); END"
        )
    )
