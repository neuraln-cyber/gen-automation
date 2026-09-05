"""Index topic claims and exhausted-attempt sweeps without rewriting outbox data.

Revision ID: 20260905_0041
Revises: 20260827_0040
Create Date: 2026-09-05

PostgreSQL builds run concurrently outside a transaction. Valid, exactly matching
indexes created by an operator before deployment are adopted; invalid or different
same-name indexes require explicit operator repair instead of an unsafe skip.
"""

from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision: str = "20260905_0041"
down_revision: str | None = "20260827_0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "outbox_events"
_INDEXES = (
    (
        "ix_outbox_topic_claim",
        ("topic", "status", "available_at", "created_at", "id"),
        None,
    ),
    (
        "ix_outbox_exhausted_claim",
        ("status", "created_at", "id"),
        "attempts >= max_attempts",
    ),
)


def _normalize_sql(value: str) -> str:
    return " ".join(value.split())


def _postgresql_schema(connection: Connection) -> str:
    schema = connection.execute(
        sa.text(
            "SELECT n.nspname FROM pg_class AS t "
            "JOIN pg_namespace AS n ON n.oid = t.relnamespace "
            "WHERE t.oid = to_regclass('outbox_events') AND t.relkind = 'r'"
        )
    ).scalar_one_or_none()
    if not isinstance(schema, str):
        raise RuntimeError("outbox index migration requires the existing outbox_events table")
    return schema


def _postgresql_index_state(
    connection: Connection, *, schema: str, name: str
) -> Mapping[str, Any] | None:
    return (
        connection.execute(
            sa.text(
                "SELECT i.indisvalid AS valid, i.indisready AS ready, i.indislive AS live, "
                "i.indrelid = to_regclass('outbox_events') AS on_expected_table, "
                "CASE WHEN i.indexrelid IS NOT NULL THEN pg_get_indexdef(c.oid) "
                "END AS definition "
                "FROM pg_class AS c "
                "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                "LEFT JOIN pg_index AS i ON i.indexrelid = c.oid "
                "WHERE n.nspname = :schema AND c.relname = :name"
            ),
            {"schema": schema, "name": name},
        )
        .mappings()
        .one_or_none()
    )


def _expected_postgresql_definition(
    connection: Connection,
    *,
    schema: str,
    name: str,
    columns: tuple[str, ...],
    predicate: str | None,
) -> str:
    quote = connection.dialect.identifier_preparer.quote
    definition = (
        f"CREATE INDEX {quote(name)} ON {quote(schema)}.{quote(_TABLE)} "
        f"USING btree ({', '.join(quote(column) for column in columns)})"
    )
    if predicate is not None:
        definition += f" WHERE ({predicate})"
    return definition


def _require_matching_postgresql_index(
    state: Mapping[str, Any], *, name: str, expected: str
) -> None:
    definition = state.get("definition")
    if (
        any(state.get(field) is not True for field in ("valid", "ready", "live"))
        or state.get("on_expected_table") is not True
        or not isinstance(definition, str)
        or _normalize_sql(definition) != expected
    ):
        raise RuntimeError(
            f"index {name} is invalid or differs from the required definition; "
            "inspect and repair it explicitly before retrying this migration"
        )


def _postgresql_change(*, create: bool) -> None:
    connection = op.get_bind()
    schema = _postgresql_schema(connection)
    existing = {}
    # Reject every conflict before creating/dropping either index. This remains
    # index-only because autocommit_block commits the current migration transaction.
    for name, columns, predicate in _INDEXES:
        state = _postgresql_index_state(connection, schema=schema, name=name)
        existing[name] = state
        if state is not None:
            _require_matching_postgresql_index(
                state,
                name=name,
                expected=_expected_postgresql_definition(
                    connection, schema=schema, name=name, columns=columns, predicate=predicate
                ),
            )
    for name, columns, predicate in _INDEXES:
        if create and existing[name] is None:
            with op.get_context().autocommit_block():
                op.create_index(
                    name,
                    _TABLE,
                    list(columns),
                    schema=schema,
                    unique=False,
                    postgresql_concurrently=True,
                    postgresql_where=sa.text(predicate) if predicate is not None else None,
                )
                state = _postgresql_index_state(connection, schema=schema, name=name)
                if state is None:
                    raise RuntimeError(f"concurrent index build did not create {name}")
                _require_matching_postgresql_index(
                    state,
                    name=name,
                    expected=_expected_postgresql_definition(
                        connection, schema=schema, name=name, columns=columns, predicate=predicate
                    ),
                )
        elif not create and existing[name] is not None:
            with op.get_context().autocommit_block():
                op.drop_index(name, table_name=_TABLE, schema=schema, postgresql_concurrently=True)


def _sqlite_change(*, create: bool) -> None:
    indexes = {item["name"]: item for item in sa.inspect(op.get_bind()).get_indexes(_TABLE)}
    for name, columns, predicate in _INDEXES:
        existing = indexes.get(name)
        if existing is None:
            continue
        actual_predicate = existing.get("dialect_options", {}).get("sqlite_where")
        if (
            existing["column_names"] != list(columns)
            or existing["unique"]
            or (_normalize_sql(str(actual_predicate)) if actual_predicate is not None else None)
            != predicate
        ):
            raise RuntimeError(f"index {name} differs from the required definition")
    for name, columns, predicate in _INDEXES:
        if create and name not in indexes:
            op.create_index(
                name,
                _TABLE,
                list(columns),
                unique=False,
                sqlite_where=sa.text(predicate) if predicate is not None else None,
            )
        elif not create and name in indexes:
            op.drop_index(name, table_name=_TABLE)


def _change(*, create: bool) -> None:
    if op.get_context().as_sql:
        raise RuntimeError("outbox index migration requires online index validation")
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        _postgresql_change(create=create)
    elif dialect == "sqlite":
        _sqlite_change(create=create)
    else:
        raise RuntimeError(f"outbox index migration does not support {dialect}")


def upgrade() -> None:
    _change(create=True)


def downgrade() -> None:
    _change(create=False)
