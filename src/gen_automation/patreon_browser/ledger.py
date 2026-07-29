from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from gen_automation.integrations.patreon.driver import (
    PatreonDriverOutcome,
    PatreonDriverResult,
)


class PatreonBrowserLedgerError(RuntimeError):
    """Persistent idempotency state is unavailable or inconsistent."""


class PatreonBrowserLedgerConflictError(PatreonBrowserLedgerError):
    """An idempotency key was reused with different immutable identity."""


@dataclass(frozen=True, slots=True)
class PatreonBrowserRequestIdentity:
    idempotency_key: str
    intent_id: UUID
    intent_digest: str
    package_id: UUID
    package_sha256: str
    profile_reference: str


class PatreonBrowserClaimState(StrEnum):
    NEW = "new"
    TERMINAL = "terminal"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class PatreonBrowserClaim:
    state: PatreonBrowserClaimState
    result: PatreonDriverResult | None = None


class PatreonBrowserIdempotencyLedger:
    """SQLite ledger that prevents a browser request from being executed twice."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS publication_requests (
                        idempotency_key TEXT PRIMARY KEY,
                        intent_id TEXT NOT NULL,
                        intent_digest TEXT NOT NULL,
                        package_id TEXT NOT NULL,
                        package_sha256 TEXT NOT NULL,
                        profile_reference TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ('started', 'terminal')),
                        outcome TEXT,
                        remote_identifier TEXT,
                        remote_url TEXT,
                        detail_code TEXT,
                        started_at TEXT NOT NULL,
                        completed_at TEXT,
                        CHECK (
                            (state = 'started' AND outcome IS NULL AND completed_at IS NULL)
                            OR
                            (
                                state = 'terminal'
                                AND outcome IS NOT NULL
                                AND completed_at IS NOT NULL
                            )
                        )
                    )
                    """
                )
            os.chmod(self._path, 0o600)
        except (OSError, sqlite3.Error) as error:
            raise PatreonBrowserLedgerError(
                "Patreon browser idempotency ledger is unavailable"
            ) from error

    def claim(self, identity: PatreonBrowserRequestIdentity) -> PatreonBrowserClaim:
        now = _timestamp()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT intent_id, intent_digest, package_id, package_sha256,
                       profile_reference, state, outcome, remote_identifier,
                       remote_url, detail_code
                  FROM publication_requests
                 WHERE idempotency_key = ?
                """,
                (identity.idempotency_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO publication_requests (
                        idempotency_key, intent_id, intent_digest, package_id,
                        package_sha256, profile_reference, state, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'started', ?)
                    """,
                    (
                        identity.idempotency_key,
                        str(identity.intent_id),
                        identity.intent_digest,
                        str(identity.package_id),
                        identity.package_sha256,
                        identity.profile_reference,
                        now,
                    ),
                )
                connection.commit()
                return PatreonBrowserClaim(state=PatreonBrowserClaimState.NEW)
            _require_matching_identity(row, identity)
            connection.commit()
            if row["state"] == "started":
                return PatreonBrowserClaim(state=PatreonBrowserClaimState.UNRESOLVED)
            return PatreonBrowserClaim(
                state=PatreonBrowserClaimState.TERMINAL,
                result=_stored_result(row),
            )
        except PatreonBrowserLedgerConflictError:
            connection.rollback()
            raise
        except (ValueError, sqlite3.Error) as error:
            connection.rollback()
            raise PatreonBrowserLedgerError(
                "Patreon browser idempotency ledger could not claim the request"
            ) from error
        finally:
            connection.close()

    def complete(
        self,
        identity: PatreonBrowserRequestIdentity,
        result: PatreonDriverResult,
    ) -> None:
        now = _timestamp()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT intent_id, intent_digest, package_id, package_sha256,
                       profile_reference, state, outcome, remote_identifier,
                       remote_url, detail_code
                  FROM publication_requests
                 WHERE idempotency_key = ?
                """,
                (identity.idempotency_key,),
            ).fetchone()
            if row is None:
                raise PatreonBrowserLedgerError(
                    "Patreon browser idempotency start record is unavailable"
                )
            _require_matching_identity(row, identity)
            if row["state"] == "terminal":
                if _stored_result(row) != result:
                    raise PatreonBrowserLedgerConflictError(
                        "Patreon browser terminal result conflicts with its ledger"
                    )
                connection.commit()
                return
            updated = connection.execute(
                """
                UPDATE publication_requests
                   SET state = 'terminal',
                       outcome = ?,
                       remote_identifier = ?,
                       remote_url = ?,
                       detail_code = ?,
                       completed_at = ?
                 WHERE idempotency_key = ? AND state = 'started'
                """,
                (
                    result.outcome.value,
                    result.remote_identifier,
                    result.remote_url,
                    result.detail_code,
                    now,
                    identity.idempotency_key,
                ),
            )
            if updated.rowcount != 1:
                raise PatreonBrowserLedgerConflictError(
                    "Patreon browser request was completed concurrently"
                )
            connection.commit()
        except PatreonBrowserLedgerError:
            connection.rollback()
            raise
        except (ValueError, sqlite3.Error) as error:
            connection.rollback()
            raise PatreonBrowserLedgerError(
                "Patreon browser idempotency ledger could not complete the request"
            ) from error
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self._path,
                timeout=5,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except sqlite3.Error as error:
            raise PatreonBrowserLedgerError(
                "Patreon browser idempotency ledger is unavailable"
            ) from error


def _require_matching_identity(
    row: sqlite3.Row,
    identity: PatreonBrowserRequestIdentity,
) -> None:
    if (
        row["intent_id"] != str(identity.intent_id)
        or row["intent_digest"] != identity.intent_digest
        or row["package_id"] != str(identity.package_id)
        or row["package_sha256"] != identity.package_sha256
        or row["profile_reference"] != identity.profile_reference
    ):
        raise PatreonBrowserLedgerConflictError(
            "Patreon browser idempotency key conflicts with another request"
        )


def _stored_result(row: sqlite3.Row) -> PatreonDriverResult:
    outcome = PatreonDriverOutcome(row["outcome"])
    return PatreonDriverResult(
        outcome=outcome,
        remote_identifier=row["remote_identifier"],
        remote_url=row["remote_url"],
        detail_code=row["detail_code"],
    )


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
