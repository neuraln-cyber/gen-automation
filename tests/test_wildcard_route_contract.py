from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from gen_automation.api.routes import wildcards as routes
from gen_automation.db.session import Database
from gen_automation.domain.wildcards import (
    WildcardAppend,
    WildcardCreate,
    WildcardReplace,
)
from gen_automation.services.wildcards import (
    WildcardConflictError,
    WildcardInputError,
    WildcardNotFoundError,
)


@pytest.mark.asyncio
async def test_wildcard_routes_map_service_failures_and_roll_back_integrity_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'wildcard-routes.db').as_posix()}")
    await database.create_schema()
    principal = SimpleNamespace(user_id=UUID(int=0))
    create = WildcardCreate(name="poses", entries=["standing"])
    replace = WildcardReplace(expected_version_no=1, entries=["sitting"])
    append = WildcardAppend(expected_version_no=1, entries=["kneeling"])

    async def expect_error(call: object, status_code: int, detail: str) -> None:
        with pytest.raises(HTTPException) as raised:
            await call  # type: ignore[misc]
        assert raised.value.status_code == status_code
        assert raised.value.detail == detail

    async def expect_integrity_rollback(call: object, session: object) -> None:
        with pytest.raises(HTTPException) as raised:
            await call  # type: ignore[misc]
        assert raised.value.status_code == 409
        assert not session.in_transaction()  # type: ignore[attr-defined]

    try:
        async with database.sessions() as session:
            monkeypatch.setattr(
                routes,
                "get_wildcard_library",
                AsyncMock(side_effect=WildcardNotFoundError("missing read")),
            )
            await expect_error(
                routes.read_wildcard("poses", session, None),  # type: ignore[arg-type]
                404,
                "missing read",
            )

            monkeypatch.setattr(
                routes,
                "create_wildcard_library",
                AsyncMock(side_effect=WildcardConflictError("duplicate create")),
            )
            await expect_error(
                routes.post_wildcard(create, session, principal),  # type: ignore[arg-type]
                409,
                "duplicate create",
            )
            await session.execute(select(1))
            monkeypatch.setattr(
                routes,
                "create_wildcard_library",
                AsyncMock(
                    side_effect=IntegrityError(
                        "insert wildcard",
                        {},
                        RuntimeError("unique race"),
                    )
                ),
            )
            await expect_integrity_rollback(
                routes.post_wildcard(create, session, principal),  # type: ignore[arg-type]
                session,
            )

            for error, status_code, detail in (
                (WildcardNotFoundError("missing replace"), 404, "missing replace"),
                (WildcardInputError("invalid replace"), 422, "invalid replace"),
                (WildcardConflictError("stale replace"), 409, "stale replace"),
            ):
                monkeypatch.setattr(
                    routes,
                    "replace_wildcard_entries",
                    AsyncMock(side_effect=error),
                )
                await expect_error(
                    routes.put_wildcard(  # type: ignore[arg-type]
                        "poses",
                        replace,
                        session,
                        principal,
                    ),
                    status_code,
                    detail,
                )
            await session.execute(select(1))
            monkeypatch.setattr(
                routes,
                "replace_wildcard_entries",
                AsyncMock(
                    side_effect=IntegrityError(
                        "replace wildcard",
                        {},
                        RuntimeError("version race"),
                    )
                ),
            )
            await expect_integrity_rollback(
                routes.put_wildcard(  # type: ignore[arg-type]
                    "poses",
                    replace,
                    session,
                    principal,
                ),
                session,
            )

            for error, status_code, detail in (
                (WildcardNotFoundError("missing append"), 404, "missing append"),
                (WildcardInputError("invalid append"), 422, "invalid append"),
                (WildcardConflictError("stale append"), 409, "stale append"),
            ):
                monkeypatch.setattr(
                    routes,
                    "append_wildcard_entries",
                    AsyncMock(side_effect=error),
                )
                await expect_error(
                    routes.post_wildcard_entries(  # type: ignore[arg-type]
                        "poses",
                        append,
                        session,
                        principal,
                    ),
                    status_code,
                    detail,
                )
            await session.execute(select(1))
            monkeypatch.setattr(
                routes,
                "append_wildcard_entries",
                AsyncMock(
                    side_effect=IntegrityError(
                        "append wildcard",
                        {},
                        RuntimeError("version race"),
                    )
                ),
            )
            await expect_integrity_rollback(
                routes.post_wildcard_entries(  # type: ignore[arg-type]
                    "poses",
                    append,
                    session,
                    principal,
                ),
                session,
            )
    finally:
        await database.dispose()
