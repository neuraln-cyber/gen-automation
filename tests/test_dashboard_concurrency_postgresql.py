"""Real PostgreSQL row-lock regression; SQLite cannot exercise FOR UPDATE."""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import delete
from sqlalchemy.dialects import postgresql

from gen_automation.auth.security import generate_opaque_token, hash_opaque_token
from gen_automation.db.models import AdminSession, AdminUser
from gen_automation.db.session import Database
from gen_automation.domain.enums import AdminRole
from gen_automation.services.authentication import SessionUnauthorizedError
from tests.test_authentication_service import NOW, _password_manager, _service
from tests.test_watermark_registry_index import registry_statement

POSTGRESQL_URL = os.getenv("GEN_AUTOMATION_TEST_POSTGRESQL_URL", "")
pytestmark = pytest.mark.skipif(
    not POSTGRESQL_URL.startswith("postgresql"),
    reason="requires the explicit PostgreSQL test database",
)


@pytest.fixture
async def browser_session() -> AsyncIterator[tuple[Database, str, AdminUser]]:
    database = Database(POSTGRESQL_URL)
    token = generate_opaque_token()
    async with database.sessions() as session:
        user = AdminUser(
            username_normalized=f"concurrency-{uuid4()}@example.test",
            display_name="Concurrency test",
            password_hash="unused-test-hash",  # noqa: S106 - no login or usable password
            role=AdminRole.OWNER,
            is_active=True,
            password_changed_at=NOW,
            credential_version=1,
        )
        session.add(user)
        await session.flush()
        session.add(
            AdminSession(
                user_id=user.id,
                token_sha256=hash_opaque_token(token),
                csrf_sha256="a" * 64,
                credential_version=1,
                created_at=NOW,
                last_seen_at=NOW,
                expires_at=NOW + timedelta(hours=1),
                idle_expires_at=NOW + timedelta(minutes=15),
                reauthenticated_at=NOW,
                mfa_verified_at=NOW,
            )
        )
        await session.commit()
    try:
        yield database, token, user
    finally:
        async with database.sessions() as session:
            await session.execute(delete(AdminSession).where(AdminSession.user_id == user.id))
            await session.execute(delete(AdminUser).where(AdminUser.id == user.id))
            await session.commit()
        await database.dispose()


@pytest.mark.parametrize("elapsed", [0, 61])
async def test_slow_handler_does_not_serialize_same_browser_requests(
    browser_session: tuple[Database, str, AdminUser], elapsed: int
) -> None:
    database, token, user = browser_session
    service = _service(_password_manager())
    now = NOW + timedelta(seconds=elapsed)
    # Keep the first request's session open, as a slow prepare/export/image route does.
    async with database.sessions() as first:
        principal = await service.resolve_session(first, session_token=token, now=now)

        async def another_request() -> None:
            async with database.sessions() as second:
                other = await service.resolve_session(second, session_token=token, now=now)
                assert other.user_id == user.id == principal.user_id

        await asyncio.wait_for(asyncio.gather(*(another_request() for _ in range(4))), 2)
        assert not first.in_transaction()


@pytest.mark.parametrize("change", ["logout", "disabled", "credentials"])
async def test_revocation_is_not_blocked_by_an_already_authenticated_slow_request(
    browser_session: tuple[Database, str, AdminUser], change: str
) -> None:
    database, token, user = browser_session
    service = _service(_password_manager())
    async with database.sessions() as first:
        principal = await service.resolve_session(first, session_token=token, now=NOW)

        async def revoke() -> None:
            async with database.sessions() as writer:
                if change == "logout":
                    stored = await writer.get(AdminSession, principal.session_id)
                    assert stored is not None
                    stored.revoked_at = NOW
                else:
                    current = await writer.get(AdminUser, user.id)
                    assert current is not None
                    if change == "disabled":
                        current.is_active = False
                    else:
                        current.credential_version += 1
                await writer.commit()

        await asyncio.wait_for(revoke(), 2)
        async with database.sessions() as second:
            with pytest.raises(SessionUnauthorizedError):
                await service.resolve_session(second, session_token=token, now=NOW)


async def test_real_registry_migration_and_generic_prepared_query_preserve_rows() -> None:
    revision = ScriptDirectory.from_config(Config("alembic.ini")).get_revision("20261111_0044")
    assert revision is not None
    migration = revision.module
    # Isolated schema, populated with synthetic rows only. No application rows are changed.
    schema = "registry_test_" + uuid4().hex
    engine = sa.create_engine(POSTGRESQL_URL, isolation_level="AUTOCOMMIT")
    statement = await registry_statement()
    compiled = statement.with_only_columns(sa.column("id"), sa.column("available_at")).compile(
        dialect=postgresql.dialect(paramstyle="numeric_dollar"),
        compile_kwargs={"render_postcompile": True},
    )
    with engine.connect() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        try:
            connection.exec_driver_sql(f'SET search_path TO "{schema}"')
            connection.exec_driver_sql(
                "CREATE TABLE assets (id uuid PRIMARY KEY, kind varchar(13), state varchar(13), "
                "available_at timestamptz, object_key varchar(1024))"
            )
            connection.exec_driver_sql(
                "INSERT INTO assets SELECT gen_random_uuid(), 'derivative', 'available', now(), "
                "'images/' || n FROM generate_series(1, 20000) AS n"
            )
            connection.exec_driver_sql(
                "INSERT INTO assets VALUES "
                "('00000000-0000-0000-0000-000000000001', 'derivative', 'available', now(), "
                "'watermarks/synthetic')"
            )
            before = connection.exec_driver_sql(
                "SELECT count(*), md5(string_agg(row_to_json(a)::text, '' ORDER BY id)) "
                "FROM assets a"
            ).one()
            connection.commit()
            context = MigrationContext.configure(connection)
            with context.begin_transaction(), Operations.context(context):
                migration.upgrade()
                migration.upgrade()  # Adopt only the same valid concurrent index.
            connection.exec_driver_sql("ANALYZE assets")
            connection.exec_driver_sql("SET plan_cache_mode = force_generic_plan")
            connection.exec_driver_sql(
                "PREPARE registry_query AS " + str(compiled),
                execution_options={"no_parameters": True},
            )
            assert [compiled.params[k] for k in compiled.positiontup] == [
                "derivative",
                "available",
                100,
            ]
            plan = connection.exec_driver_sql(
                "EXPLAIN (FORMAT JSON) EXECUTE registry_query('derivative', 'available', 100)"
            ).scalar_one()[0]["Plan"]

            def nodes(node):
                yield node
                for child in node.get("Plans", []):
                    yield from nodes(child)

            assert any(
                n.get("Index Name") == "ix_assets_registered_watermarks" for n in nodes(plan)
            )
            assert not any(n["Node Type"] in {"Seq Scan", "Sort"} for n in nodes(plan))
            rows = connection.exec_driver_sql(
                "EXECUTE registry_query('derivative', 'available', 100)"
            ).all()
            assert len(rows) == 1 and str(rows[0][0]) == "00000000-0000-0000-0000-000000000001"
            connection.commit()
            context = MigrationContext.configure(connection)
            with context.begin_transaction(), Operations.context(context):
                migration.downgrade()
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*), md5(string_agg(row_to_json(a)::text, '' ORDER BY id)) "
                    "FROM assets a"
                ).one()
                == before
            )
        finally:
            connection.exec_driver_sql("DEALLOCATE ALL")
            connection.exec_driver_sql("RESET search_path")
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
    engine.dispose()
