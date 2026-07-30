import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from gen_automation.db.models import (
    AdminUser,
    AuditEvent,
    WildcardLibrary,
    WildcardLibraryVersion,
)
from gen_automation.db.session import Database
from gen_automation.domain.enums import AdminRole
from gen_automation.wildcard_import_cli import (
    WildcardImportError,
    WildcardImportPlan,
    apply_wildcard_import_plan,
    parse_wildcard_import_plan,
    prepare_wildcard_sources,
)

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def _plan(source_path: str = "poses.txt") -> WildcardImportPlan:
    return WildcardImportPlan.model_validate(
        {
            "version": "v1",
            "owner_username": "owner@example.test",
            "libraries": [
                {
                    "source_path": source_path,
                    "library_name": "poses",
                }
            ],
        }
    )


async def _database(tmp_path: Path) -> tuple[Database, AdminUser]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'wildcards.db').as_posix()}")
    await database.create_schema()
    owner = AdminUser(
        username_normalized="owner@example.test",
        display_name="Owner",
        password_hash="disabled-test-password-hash",  # noqa: S106
        role=AdminRole.OWNER,
        is_active=True,
        failed_login_count=0,
        password_changed_at=NOW,
        credential_version=1,
        lock_version=1,
    )
    async with database.sessions() as session:
        session.add(owner)
        await session.commit()
    return database, owner


def test_plan_rejects_duplicate_json_keys_and_duplicate_mappings() -> None:
    with pytest.raises(WildcardImportError, match="plan is invalid"):
        parse_wildcard_import_plan(
            b'{"version":"v1","version":"v1","owner_username":"owner","libraries":[]}'
        )

    raw = json.dumps(
        {
            "version": "v1",
            "owner_username": "owner",
            "libraries": [
                {"source_path": "first.txt", "library_name": "poses"},
                {"source_path": "second.txt", "library_name": "poses"},
            ],
        }
    ).encode()
    with pytest.raises(WildcardImportError, match="plan is invalid"):
        parse_wildcard_import_plan(raw)


def test_source_preflight_preserves_duplicates_and_whitespace(tmp_path: Path) -> None:
    source = tmp_path / "poses.txt"
    source.write_text(
        "  first pose  \nduplicate\n\n  \nduplicate\n",
        encoding="utf-8",
    )

    prepared = prepare_wildcard_sources(_plan(), plan_directory=tmp_path)

    assert prepared[0].entries == ("  first pose  ", "duplicate", "duplicate")
    assert prepared[0].source_line_count == 5
    assert prepared[0].dropped_blank_count == 2


def test_source_preflight_rejects_missing_and_all_blank_entries(tmp_path: Path) -> None:
    with pytest.raises(WildcardImportError, match="is missing"):
        prepare_wildcard_sources(_plan(), plan_directory=tmp_path)

    (tmp_path / "poses.txt").write_text("\n  \n", encoding="utf-8")
    with pytest.raises(WildcardImportError, match=r"source entries.*invalid"):
        prepare_wildcard_sources(_plan(), plan_directory=tmp_path)


async def test_import_is_versioned_audited_dry_runnable_and_idempotent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "poses.txt"
    source.write_text("  first pose  \nduplicate\n\nduplicate\n", encoding="utf-8")
    database, owner = await _database(tmp_path)
    plan = _plan()
    try:
        async with database.sessions() as session:
            created = await apply_wildcard_import_plan(
                session,
                plan=plan,
                plan_directory=tmp_path,
                dry_run=False,
            )
            unchanged = await apply_wildcard_import_plan(
                session,
                plan=plan,
                plan_directory=tmp_path,
                dry_run=False,
            )

        assert created[0].action == "created"
        assert created[0].version_no == 1
        assert created[0].source_line_count == 4
        assert created[0].dropped_blank_count == 1
        assert unchanged[0].action == "unchanged"
        assert unchanged[0].version_no == 1

        source.write_text(
            "  first pose  \nduplicate\n\nduplicate\nnew pose",
            encoding="utf-8",
        )
        async with database.sessions() as session:
            preview = await apply_wildcard_import_plan(
                session,
                plan=plan,
                plan_directory=tmp_path,
                dry_run=True,
            )
            version_count_after_preview = int(
                await session.scalar(select(func.count()).select_from(WildcardLibraryVersion)) or 0
            )
            audit_count_after_preview = int(
                await session.scalar(select(func.count()).select_from(AuditEvent)) or 0
            )

        assert preview[0].action == "would_update"
        assert preview[0].version_no == 2
        assert preview[0].source_line_count == 5
        assert preview[0].dropped_blank_count == 1
        assert version_count_after_preview == 1
        assert audit_count_after_preview == 1

        async with database.sessions() as session:
            updated = await apply_wildcard_import_plan(
                session,
                plan=plan,
                plan_directory=tmp_path,
                dry_run=False,
            )
            replay = await apply_wildcard_import_plan(
                session,
                plan=plan,
                plan_directory=tmp_path,
                dry_run=False,
            )
            library = await session.scalar(
                select(WildcardLibrary).where(WildcardLibrary.name == "poses")
            )
            versions = tuple(
                (
                    await session.scalars(
                        select(WildcardLibraryVersion)
                        .where(WildcardLibraryVersion.library_id == library.id)
                        .order_by(WildcardLibraryVersion.version_no)
                    )
                ).all()
            )
            audits = tuple(
                (
                    await session.scalars(
                        select(AuditEvent)
                        .where(AuditEvent.resource_type == "wildcard_library")
                        .order_by(AuditEvent.occurred_at)
                    )
                ).all()
            )

        assert updated[0].action == "updated"
        assert updated[0].version_no == 2
        assert replay[0].action == "unchanged"
        assert replay[0].version_no == 2
        assert library is not None
        assert len(versions) == 2
        assert versions[1].entries == [
            "  first pose  ",
            "duplicate",
            "duplicate",
            "new pose",
        ]
        assert [event.action for event in audits] == [
            "wildcard_library.created",
            "wildcard_library.entries_replaced",
        ]
        assert {event.actor for event in audits} == {str(owner.id)}
    finally:
        await database.dispose()
