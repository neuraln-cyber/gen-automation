"""Retire resumable legacy JPEG MEGA set deliveries.

Revision ID: 20260809_0031
Revises: 20260809_0030
Create Date: 2026-08-09

The migration is deliberately metadata-only.  It preserves every remote path,
uploaded item, node handle, counter, and source archive while preventing an old
lossy delivery from resuming after the public PNG pipeline is deployed.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "20260809_0031"
down_revision: str | None = "20260809_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_PROFILE = "legacy-full-derivative-v1"
_RETIREMENT_ERROR_CODE = "mega_set_legacy_media_retired"
_RETIREMENT_DETAIL = "Legacy lossy MEGA delivery was retired; existing remote files were preserved."
_NONTERMINAL_STATES = ("pending", "claimed", "retry_wait")


def _tables() -> tuple[sa.TableClause, sa.TableClause]:
    archives = sa.table(
        "finished_set_archives",
        sa.column("id"),
        sa.column("media_profile"),
    )
    deliveries = sa.table(
        "mega_set_deliveries",
        sa.column("finished_set_archive_id"),
        sa.column("state"),
        sa.column("available_at"),
        sa.column("lease_owner"),
        sa.column("lease_expires_at"),
        sa.column("completion_marker_node_handle"),
        sa.column("verified_at"),
        sa.column("completed_at"),
        sa.column("last_error_code"),
        sa.column("last_error_detail"),
        sa.column("updated_at"),
    )
    return archives, deliveries


def upgrade() -> None:
    archives, deliveries = _tables()
    retired_at = datetime.now(UTC)
    legacy_archive_ids = sa.select(archives.c.id).where(archives.c.media_profile == _LEGACY_PROFILE)
    op.get_bind().execute(
        deliveries.update()
        .where(
            deliveries.c.finished_set_archive_id.in_(legacy_archive_ids),
            deliveries.c.state.in_(_NONTERMINAL_STATES),
        )
        .values(
            state="failed",
            available_at=retired_at,
            lease_owner=None,
            lease_expires_at=None,
            completion_marker_node_handle=None,
            verified_at=None,
            completed_at=retired_at,
            last_error_code=_RETIREMENT_ERROR_CODE,
            last_error_detail=_RETIREMENT_DETAIL,
            updated_at=retired_at,
        )
    )


def downgrade() -> None:
    # Retirement is intentionally data-irreversible. Reopening a lossy
    # delivery during code rollback could restart an external JPEG upload, and
    # the previous state/schedule/error cannot be reconstructed truthfully.
    # No schema object was added by this revision, so retaining the terminal
    # rows is the only safe downgrade behavior.
    return None
