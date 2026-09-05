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

from gen_automation.db.models import OutboxEvent


@pytest.fixture
def migration():
    revision = ScriptDirectory.from_config(Config("alembic.ini")).get_revision("20260905_0041")
    assert revision is not None
    assert revision.down_revision == "20260827_0040"
    return revision.module


def test_outbox_poll_index_metadata_matches_queries_and_preserves_old_index() -> None:
    indexes = {index.name: index for index in OutboxEvent.__table__.indexes}
    assert "ix_outbox_claim" in indexes
    claim = indexes["ix_outbox_topic_claim"]
    exhausted = indexes["ix_outbox_exhausted_claim"]
    assert [column.name for column in claim.columns] == [
        "topic",
        "status",
        "available_at",
        "created_at",
        "id",
    ]
    assert [column.name for column in exhausted.columns] == ["status", "created_at", "id"]
    assert str(exhausted.dialect_options["postgresql"]["where"]) == "attempts >= max_attempts"
    assert str(exhausted.dialect_options["sqlite"]["where"]) == "attempts >= max_attempts"
    assert str(CreateIndex(exhausted).compile(dialect=postgresql.dialect())).endswith(
        "(status, created_at, id) WHERE attempts >= max_attempts"
    )


@pytest.mark.parametrize("existing", ["missing", "matching", "mismatched"])
def test_sqlite_index_migration_preserves_rows_and_adopts_only_matching_indexes(
    migration, existing: str
) -> None:
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table(
        "outbox_events",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("topic", sa.String),
        sa.Column("status", sa.String),
        sa.Column("available_at", sa.Integer),
        sa.Column("lease_expires_at", sa.Integer),
        sa.Column("created_at", sa.Integer),
        sa.Column("attempts", sa.Integer),
        sa.Column("max_attempts", sa.Integer),
        sa.Index("ix_outbox_claim", "status", "available_at", "lease_expires_at", "created_at"),
    )
    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(
            table.insert(),
            [
                {
                    "id": 1,
                    "topic": "asset.available",
                    "status": "pending",
                    "attempts": 0,
                    "max_attempts": 10,
                }
            ],
        )
        before = list(connection.execute(sa.select(table)))
        with Operations.context(MigrationContext.configure(connection)):
            if existing == "matching":
                migration.upgrade()
            elif existing == "mismatched":
                Operations(MigrationContext.configure(connection)).create_index(
                    "ix_outbox_topic_claim", "outbox_events", ["status"]
                )
            if existing == "mismatched":
                with pytest.raises(RuntimeError, match="differs"):
                    migration.upgrade()
            else:
                migration.upgrade()
                indexes = {
                    item["name"]: item for item in sa.inspect(connection).get_indexes(table.name)
                }
                assert set(indexes) == {
                    "ix_outbox_claim",
                    "ix_outbox_topic_claim",
                    "ix_outbox_exhausted_claim",
                }
                assert str(
                    indexes["ix_outbox_exhausted_claim"]["dialect_options"]["sqlite_where"]
                ) == ("attempts >= max_attempts")
                migration.downgrade()
                assert [
                    item["name"] for item in sa.inspect(connection).get_indexes(table.name)
                ] == ["ix_outbox_claim"]
        assert list(connection.execute(sa.select(table))) == before
    engine.dispose()


@pytest.mark.parametrize(
    "existing", ["missing", "matching", "valid", "ready", "live", "on_expected_table", "definition"]
)
def test_postgresql_indexes_are_concurrent_and_adopt_only_valid_exact_definitions(
    migration, monkeypatch, existing: str
) -> None:
    connection = SimpleNamespace(dialect=postgresql.dialect())
    states = {}
    expected = {}
    for name, columns, predicate in migration._INDEXES:
        expected[name] = migration._expected_postgresql_definition(
            connection, schema="public", name=name, columns=columns, predicate=predicate
        )
        if existing != "missing":
            states[name] = {
                "valid": True,
                "ready": True,
                "live": True,
                "on_expected_table": True,
                "definition": expected[name],
            }
    if existing not in {"missing", "matching"}:
        # A conflict in the second index must prevent changes to either one.
        states["ix_outbox_exhausted_claim"][existing] = (
            expected["ix_outbox_exhausted_claim"] + " WHERE status = 'pending'"
            if existing == "definition"
            else False
        )
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    monkeypatch.setattr(migration, "_postgresql_schema", lambda _connection: "public")
    monkeypatch.setattr(
        migration, "_postgresql_index_state", lambda _connection, *, schema, name: states.get(name)
    )
    inside_autocommit = [False]
    transaction_entries = []

    @contextmanager
    def autocommit_block():
        assert not inside_autocommit[0]
        inside_autocommit[0] = True
        transaction_entries.append(True)
        try:
            yield
        finally:
            inside_autocommit[0] = False

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
        assert inside_autocommit[0]
        assert kwargs["postgresql_concurrently"] is True
        ddl.create_index(name, table, columns, **kwargs)
        states[name] = {
            "valid": True,
            "ready": True,
            "live": True,
            "on_expected_table": True,
            "definition": expected[name],
        }

    monkeypatch.setattr(migration.op, "create_index", create_index)
    if existing not in {"missing", "matching"}:
        with pytest.raises(RuntimeError, match="invalid or differs"):
            migration.upgrade()
        assert transaction_entries == []
        assert emitted.getvalue() == ""
        return
    migration.upgrade()
    if existing == "matching":
        assert transaction_entries == []
        assert emitted.getvalue() == ""
    else:
        assert len(transaction_entries) == 2
        assert emitted.getvalue().count("CREATE INDEX CONCURRENTLY") == 2
        assert "WHERE attempts >= max_attempts" in emitted.getvalue()
        assert "IF NOT EXISTS" not in emitted.getvalue()

    def drop_index(name, **kwargs):
        assert inside_autocommit[0]
        assert kwargs["postgresql_concurrently"] is True
        ddl.drop_index(name, **kwargs)

    monkeypatch.setattr(migration.op, "drop_index", drop_index)
    migration.downgrade()
    assert emitted.getvalue().count("DROP INDEX CONCURRENTLY") == 2
    assert "ix_outbox_claim" not in emitted.getvalue()
