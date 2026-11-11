from contextlib import contextmanager
from io import StringIO
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from gen_automation.db.models import Asset
from gen_automation.services.watermarks import list_registered_watermarks


@pytest.fixture
def migration():
    revision = ScriptDirectory.from_config(Config("alembic.ini")).get_revision("20261111_0044")
    assert revision is not None
    assert revision.down_revision == "20260925_0043"
    return revision.module


async def registry_statement():
    captured = []

    async def scalars(statement):
        captured.append(statement)
        return SimpleNamespace(all=lambda: [])

    assert await list_registered_watermarks(SimpleNamespace(scalars=scalars)) == ()
    return captured[0]


async def test_registry_prefix_is_literal_but_parameters_remain_bound() -> None:
    compiled = (await registry_statement()).compile(
        dialect=postgresql.dialect(), compile_kwargs={"render_postcompile": True}
    )
    assert "LIKE 'watermarks/%%'" in str(compiled)
    assert "derivative" not in str(compiled)
    assert set(compiled.params.values()) == {"derivative", "available", 100}
    index = next(i for i in Asset.__table__.indexes if i.name == "ix_assets_registered_watermarks")
    assert [c.name for c in index.columns] == ["kind", "state", "available_at", "id"]
    assert str(CreateIndex(index).compile(dialect=postgresql.dialect())).endswith(
        "(kind, state, available_at, id) WHERE object_key LIKE 'watermarks/%%'"
    )


@pytest.mark.parametrize("existing", ["missing", "matching", "mismatched"])
def test_sqlite_migration_preserves_assets_and_adopts_only_matching_index(migration, existing):
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table(
        "assets",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("kind", sa.String),
        sa.Column("state", sa.String),
        sa.Column("available_at", sa.Integer),
        sa.Column("object_key", sa.String),
        sa.Index("ix_existing_asset", "object_key"),
    )
    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(
            table.insert(),
            [
                {
                    "id": 1,
                    "kind": "derivative",
                    "state": "available",
                    "object_key": "watermarks/test",
                },
                {"id": 2, "kind": "raw_master", "state": "available", "object_key": "images/test"},
            ],
        )
        before = list(connection.execute(sa.select(table)))
        with Operations.context(MigrationContext.configure(connection)):
            if existing == "matching":
                migration.upgrade()
            elif existing == "mismatched":
                connection.exec_driver_sql(
                    "CREATE INDEX ix_assets_registered_watermarks ON assets(id)"
                )
            if existing == "mismatched":
                with pytest.raises(RuntimeError, match="differs"):
                    migration.upgrade()
            else:
                migration.upgrade()
                assert {i["name"] for i in sa.inspect(connection).get_indexes("assets")} == {
                    "ix_assets_registered_watermarks",
                    "ix_existing_asset",
                }
                migration.downgrade()
                assert [i["name"] for i in sa.inspect(connection).get_indexes("assets")] == [
                    "ix_existing_asset"
                ]
        assert list(connection.execute(sa.select(table))) == before
    engine.dispose()


@pytest.mark.parametrize(
    "existing", ["missing", "matching", "valid", "ready", "live", "on_expected_table", "definition"]
)
def test_postgres_build_is_concurrent_and_rejects_conflicts(migration, monkeypatch, existing):
    connection = SimpleNamespace(dialect=postgresql.dialect())
    name, columns, predicate = migration._INDEXES[0]
    expected = migration._expected_postgresql_definition(
        connection, schema="public", name=name, columns=columns, predicate=predicate
    )
    state = {
        "valid": True,
        "ready": True,
        "live": True,
        "on_expected_table": True,
        "definition": expected,
    }
    states = {} if existing == "missing" else {name: state}
    if existing not in {"missing", "matching"}:
        state[existing] = expected + " unexpected" if existing == "definition" else False
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    monkeypatch.setattr(migration, "_postgresql_schema", lambda _: "public")
    monkeypatch.setattr(migration, "_postgresql_index_state", lambda *a, **k: states.get(name))
    inside = []

    @contextmanager
    def autocommit_block():
        inside.append(True)
        try:
            yield
        finally:
            inside.pop()

    monkeypatch.setattr(
        migration.op,
        "get_context",
        lambda: SimpleNamespace(as_sql=False, autocommit_block=autocommit_block),
    )
    emitted = StringIO()
    ddl = Operations(
        MigrationContext.configure(
            dialect_name="postgresql", opts={"as_sql": True, "output_buffer": emitted}
        )
    )

    def create_index(name, table, columns, **kwargs):
        assert inside and kwargs["postgresql_concurrently"] is True
        ddl.create_index(name, table, columns, **kwargs)
        states[name] = state

    def drop_index(name, **kwargs):
        assert inside and kwargs["postgresql_concurrently"] is True
        ddl.drop_index(name, **kwargs)

    monkeypatch.setattr(migration.op, "create_index", create_index)
    monkeypatch.setattr(migration.op, "drop_index", drop_index)
    if existing not in {"missing", "matching"}:
        with pytest.raises(RuntimeError, match="invalid or differs"):
            migration.upgrade()
        assert emitted.getvalue() == ""
    else:
        migration.upgrade()
        assert emitted.getvalue().count("CREATE INDEX CONCURRENTLY") == (existing == "missing")
        migration.downgrade()
        assert emitted.getvalue().count("DROP INDEX CONCURRENTLY") == 1
