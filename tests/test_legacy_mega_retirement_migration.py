from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, create_engine, select

_PREVIOUS = "20260809_0030"
_MIGRATION = "20260809_0031"
_LEGACY_PROFILE = "legacy-full-derivative-v1"
_PUBLIC_PROFILE = "public-png-v1"
_RETIREMENT_CODE = "mega_set_legacy_media_retired"
_NOW = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)


def _configuration(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv(
        "GEN_AUTOMATION_DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )
    return Config("alembic.ini")


def _seed(database_path: Path) -> dict[str, str]:
    identities = {
        "legacy_archive": uuid4().hex,
        "legacy_terminal_archive": uuid4().hex,
        "public_archive": uuid4().hex,
        "legacy_retry": uuid4().hex,
        "legacy_terminal": uuid4().hex,
        "public_retry": uuid4().hex,
        "legacy_item": uuid4().hex,
    }
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    metadata = MetaData()
    metadata.reflect(
        bind=engine,
        only=["finished_set_archives", "mega_set_deliveries", "mega_set_delivery_items"],
    )
    archives = metadata.tables["finished_set_archives"]
    deliveries = metadata.tables["mega_set_deliveries"]
    items = metadata.tables["mega_set_delivery_items"]
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        connection.commit()
        for archive_id, profile in (
            (identities["legacy_archive"], _LEGACY_PROFILE),
            (identities["legacy_terminal_archive"], _LEGACY_PROFILE),
            (identities["public_archive"], _PUBLIC_PROFILE),
        ):
            connection.execute(
                archives.insert(),
                {
                    "id": archive_id,
                    "review_task_id": uuid4().hex,
                    "release_version_id": uuid4().hex,
                    "media_profile": profile,
                    "state": "ready",
                    "selection_count": 199,
                    "manifest_sha256": "a" * 64,
                    "part_count": 1,
                    "attempts": 1,
                    "max_attempts": 5,
                    "available_at": _NOW,
                    "created_at": _NOW,
                    "updated_at": _NOW,
                    "started_at": _NOW,
                    "completed_at": _NOW,
                },
            )

        def delivery_values(
            delivery_id: str,
            archive_id: str,
            *,
            state: str,
            folder: str,
        ) -> dict[str, object]:
            terminal = state == "failed"
            return {
                "id": delivery_id,
                "finished_set_archive_id": archive_id,
                "state": state,
                "remote_root": "/Future",
                "remote_folder": folder,
                "manifest_sha256": "a" * 64,
                "total_item_count": 199,
                "uploaded_item_count": 100,
                "uploaded_byte_size": 0,
                "attempts": 14,
                "available_at": _NOW,
                "completed_at": _NOW if terminal else None,
                "last_error_code": "already_terminal" if terminal else "transport_retryable",
                "last_error_detail": "safe prior status",
                "created_at": _NOW,
                "updated_at": _NOW,
            }

        connection.execute(
            deliveries.insert(),
            [
                delivery_values(
                    identities["legacy_retry"],
                    identities["legacy_archive"],
                    state="retry_wait",
                    folder="/Future/Akali (NSFW)",
                ),
                delivery_values(
                    identities["legacy_terminal"],
                    identities["legacy_terminal_archive"],
                    state="failed",
                    folder="/Future/terminal-legacy",
                ),
                delivery_values(
                    identities["public_retry"],
                    identities["public_archive"],
                    state="retry_wait",
                    folder="/Future/Akali (NSFW) (PNG)",
                ),
            ],
        )
        connection.execute(
            items.insert(),
            {
                "id": identities["legacy_item"],
                "delivery_id": identities["legacy_retry"],
                "ordinal": 101,
                "source_asset_id": uuid4().hex,
                "readiness_derivative_output_id": uuid4().hex,
                "source_sha256": "b" * 64,
                "source_byte_size": 1234,
                "source_content_type": "image/jpeg",
                "remote_path": "/Future/Akali (NSFW)/101.jpg",
                "state": "pending",
                "attempts": 0,
                "available_at": _NOW,
                "created_at": _NOW,
                "updated_at": _NOW,
            },
        )
        connection.commit()
    engine.dispose()
    return identities


def test_legacy_retirement_is_narrow_and_never_reopens_on_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "legacy-mega-retirement.db"
    configuration = _configuration(database_path, monkeypatch)
    command.upgrade(configuration, _PREVIOUS)
    identities = _seed(database_path)

    command.upgrade(configuration, _MIGRATION)
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    metadata = MetaData()
    metadata.reflect(
        bind=engine,
        only=["mega_set_deliveries", "mega_set_delivery_items"],
    )
    deliveries = metadata.tables["mega_set_deliveries"]
    items = metadata.tables["mega_set_delivery_items"]
    with engine.connect() as connection:
        rows = {row.id: row for row in connection.execute(select(deliveries)).mappings()}
        item = (
            connection.execute(select(items).where(items.c.id == identities["legacy_item"]))
            .mappings()
            .one()
        )

    retired = rows[identities["legacy_retry"]]
    assert retired.state == "failed"
    assert retired.last_error_code == _RETIREMENT_CODE
    assert retired.completed_at is not None
    assert retired.uploaded_item_count == 100
    assert retired.remote_folder == "/Future/Akali (NSFW)"
    assert item.state == "pending"
    assert item.attempts == 0
    assert item.remote_path == "/Future/Akali (NSFW)/101.jpg"
    assert rows[identities["legacy_terminal"]].last_error_code == "already_terminal"
    assert rows[identities["public_retry"]].state == "retry_wait"
    assert rows[identities["public_retry"]].last_error_code == "transport_retryable"

    command.downgrade(configuration, _PREVIOUS)
    with engine.connect() as connection:
        rows = {row.id: row for row in connection.execute(select(deliveries)).mappings()}
    assert rows[identities["legacy_retry"]].state == "failed"
    assert rows[identities["legacy_retry"]].last_error_code == _RETIREMENT_CODE
    assert rows[identities["legacy_retry"]].completed_at is not None
    assert rows[identities["legacy_terminal"]].last_error_code == "already_terminal"
    assert rows[identities["public_retry"]].state == "retry_wait"
    engine.dispose()
