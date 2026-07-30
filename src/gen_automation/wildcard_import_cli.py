import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import AdminUser
from gen_automation.db.session import Database
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import AdminRole
from gen_automation.domain.release_spec import WildcardName
from gen_automation.domain.wildcards import (
    MAX_WILDCARD_ENTRIES,
    MAX_WILDCARD_ENTRIES_BYTES,
    MAX_WILDCARD_LIBRARIES,
    WildcardCreate,
    WildcardReplace,
)
from gen_automation.services.authentication import normalize_username
from gen_automation.services.wildcards import (
    WildcardError,
    WildcardNotFoundError,
    create_wildcard_library,
    get_wildcard_library,
    replace_wildcard_entries,
)

MAX_PLAN_BYTES = 256 * 1024
MAX_SOURCE_BYTES = MAX_WILDCARD_ENTRIES_BYTES + (MAX_WILDCARD_ENTRIES * 2) + 3


class WildcardImportError(Exception):
    """Safe, operator-facing wildcard import failure."""


class _StrictImportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class WildcardSourceMapping(_StrictImportModel):
    source_path: str = Field(min_length=1, max_length=1_024)
    library_name: WildcardName

    @field_validator("source_path")
    @classmethod
    def validate_source_path(cls, value: str) -> str:
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("wildcard source path must be trimmed text")
        return value


class WildcardImportPlan(_StrictImportModel):
    version: Literal["v1"]
    owner_username: str = Field(min_length=1, max_length=200)
    libraries: list[WildcardSourceMapping] = Field(
        min_length=1,
        max_length=MAX_WILDCARD_LIBRARIES,
    )

    @field_validator("owner_username")
    @classmethod
    def validate_owner_username(cls, value: str) -> str:
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("owner username must be trimmed visible text")
        return value

    @field_validator("libraries")
    @classmethod
    def require_unique_mappings(
        cls,
        libraries: list[WildcardSourceMapping],
    ) -> list[WildcardSourceMapping]:
        names = [entry.library_name for entry in libraries]
        sources = [entry.source_path.casefold() for entry in libraries]
        if len(names) != len(set(names)):
            raise ValueError("wildcard library names must be unique")
        if len(sources) != len(set(sources)):
            raise ValueError("wildcard source paths must be unique")
        return libraries


class WildcardImportSettings(BaseSettings):
    """Minimum secret scope for the one-off wildcard import job."""

    model_config = SettingsConfigDict(
        env_prefix="GEN_AUTOMATION_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
        hide_input_in_errors=True,
    )

    database_url: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class PreparedWildcard:
    source_path: Path
    library_name: str
    entries: tuple[str, ...]
    entries_sha256: str
    source_line_count: int
    dropped_blank_count: int


@dataclass(frozen=True, slots=True)
class WildcardImportResult:
    library_name: str
    action: Literal[
        "created",
        "updated",
        "unchanged",
        "would_create",
        "would_update",
    ]
    version_no: int
    entry_count: int
    entries_sha256: str
    source_line_count: int
    dropped_blank_count: int


def parse_wildcard_import_plan(raw: bytes) -> WildcardImportPlan:
    if not raw or len(raw) > MAX_PLAN_BYTES:
        raise WildcardImportError("wildcard import plan size is invalid")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
        return WildcardImportPlan.model_validate(value)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValidationError,
        ValueError,
    ):
        raise WildcardImportError("wildcard import plan is invalid") from None


def prepare_wildcard_sources(
    plan: WildcardImportPlan,
    *,
    plan_directory: Path,
) -> tuple[PreparedWildcard, ...]:
    prepared: list[PreparedWildcard] = []
    resolved_sources: set[Path] = set()
    for mapping in plan.libraries:
        source = Path(mapping.source_path)
        if not source.is_absolute():
            source = plan_directory / source
        try:
            source = source.resolve(strict=True)
        except OSError:
            raise WildcardImportError(
                f"source file for wildcard '{mapping.library_name}' is missing"
            ) from None
        if not source.is_file():
            raise WildcardImportError(
                f"source for wildcard '{mapping.library_name}' is not a regular file"
            )
        if source in resolved_sources:
            raise WildcardImportError("wildcard source files must be unique")
        resolved_sources.add(source)
        source_lines = _read_source_lines(source, library_name=mapping.library_name)
        entries = [line for line in source_lines if line.strip()]
        try:
            command = WildcardCreate(
                name=mapping.library_name,
                entries=entries,
            )
        except ValidationError:
            raise WildcardImportError(
                f"source entries for wildcard '{mapping.library_name}' are invalid"
            ) from None
        prepared.append(
            PreparedWildcard(
                source_path=source,
                library_name=command.name,
                entries=tuple(command.entries),
                entries_sha256=canonical_sha256(command.entries),
                source_line_count=len(source_lines),
                dropped_blank_count=len(source_lines) - len(command.entries),
            )
        )
    return tuple(prepared)


async def apply_wildcard_import_plan(
    session: AsyncSession,
    *,
    plan: WildcardImportPlan,
    plan_directory: Path,
    dry_run: bool,
) -> tuple[WildcardImportResult, ...]:
    prepared = prepare_wildcard_sources(plan, plan_directory=plan_directory)
    owner = await _active_owner(session, username=plan.owner_username)
    results: list[WildcardImportResult] = []
    for item in prepared:
        entries = list(item.entries)
        try:
            current = await get_wildcard_library(session, name=item.library_name)
        except WildcardNotFoundError:
            if dry_run:
                results.append(
                    WildcardImportResult(
                        library_name=item.library_name,
                        action="would_create",
                        version_no=1,
                        entry_count=len(entries),
                        entries_sha256=item.entries_sha256,
                        source_line_count=item.source_line_count,
                        dropped_blank_count=item.dropped_blank_count,
                    )
                )
                continue
            written = await create_wildcard_library(
                session,
                command=WildcardCreate(name=item.library_name, entries=entries),
                actor=str(owner.id),
            )
            action: Literal["created", "updated", "unchanged"] = "created"
        else:
            if current.entries == entries:
                results.append(
                    WildcardImportResult(
                        library_name=item.library_name,
                        action="unchanged",
                        version_no=current.current_version_no,
                        entry_count=len(entries),
                        entries_sha256=item.entries_sha256,
                        source_line_count=item.source_line_count,
                        dropped_blank_count=item.dropped_blank_count,
                    )
                )
                continue
            if dry_run:
                results.append(
                    WildcardImportResult(
                        library_name=item.library_name,
                        action="would_update",
                        version_no=current.current_version_no + 1,
                        entry_count=len(entries),
                        entries_sha256=item.entries_sha256,
                        source_line_count=item.source_line_count,
                        dropped_blank_count=item.dropped_blank_count,
                    )
                )
                continue
            written = await replace_wildcard_entries(
                session,
                name=item.library_name,
                command=WildcardReplace(
                    expected_version_no=current.current_version_no,
                    entries=entries,
                ),
                actor=str(owner.id),
            )
            action = "updated"
        results.append(
            WildcardImportResult(
                library_name=item.library_name,
                action=action,
                version_no=written.current_version_no,
                entry_count=len(written.entries),
                entries_sha256=written.entries_sha256,
                source_line_count=item.source_line_count,
                dropped_blank_count=item.dropped_blank_count,
            )
        )
    return tuple(results)


def wildcard_import_main() -> int:
    parser = argparse.ArgumentParser(
        prog="gen-automation-wildcards",
        description="Import exact wildcard text files into versioned libraries.",
    )
    parser.add_argument("plan", type=Path, help="Non-secret wildcard import plan JSON.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and compare every source without changing the database.",
    )
    arguments = parser.parse_args()

    try:
        plan_path = arguments.plan.resolve(strict=True)
        plan = parse_wildcard_import_plan(_read_bounded(plan_path, MAX_PLAN_BYTES))
        settings = WildcardImportSettings()
        results = asyncio.run(
            _apply_plan(
                settings=settings,
                plan=plan,
                plan_directory=plan_path.parent,
                dry_run=arguments.dry_run,
            )
        )
    except WildcardImportError as error:
        print(f"Wildcard import failed: {error}", file=sys.stderr)
        return 1
    except WildcardError as error:
        print(f"Wildcard import failed: {error}", file=sys.stderr)
        return 1
    except IntegrityError:
        print(
            "Wildcard import conflicted with a concurrent database change; rerun the plan.",
            file=sys.stderr,
        )
        return 1
    except ValidationError:
        print(
            "Wildcard import configuration is invalid. Supply the database URL "
            "through the job environment/secret identity.",
            file=sys.stderr,
        )
        return 2
    except OSError:
        print(
            "Wildcard import failed while reading a local file.",
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            "Wildcard import failed without exposing database or secret details. "
            "Check restricted service logs.",
            file=sys.stderr,
        )
        return 1

    prefix = "Dry run" if arguments.dry_run else "Import"
    print(f"{prefix} completed for {len(results)} wildcard libraries.")
    for result in results:
        print(
            f"{result.library_name}: {result.action}; version={result.version_no}; "
            f"source_lines={result.source_line_count}; "
            f"dropped_blank={result.dropped_blank_count}; "
            f"entries={result.entry_count}; sha256={result.entries_sha256}"
        )
    return 0


async def _apply_plan(
    *,
    settings: WildcardImportSettings,
    plan: WildcardImportPlan,
    plan_directory: Path,
    dry_run: bool,
) -> tuple[WildcardImportResult, ...]:
    database = Database(settings.database_url)
    try:
        await database.ping()
        async with database.sessions() as session:
            return await apply_wildcard_import_plan(
                session,
                plan=plan,
                plan_directory=plan_directory,
                dry_run=dry_run,
            )
    finally:
        await database.dispose()


async def _active_owner(session: AsyncSession, *, username: str) -> AdminUser:
    try:
        normalized_username = normalize_username(username)
    except ValueError:
        raise WildcardImportError("owner_username is invalid") from None
    owner = await session.scalar(
        select(AdminUser).where(
            AdminUser.username_normalized == normalized_username,
            AdminUser.role == AdminRole.OWNER,
            AdminUser.is_active.is_(True),
        )
    )
    if owner is None:
        raise WildcardImportError("the selected active owner account was not found")
    return owner


def _read_source_lines(path: Path, *, library_name: str) -> list[str]:
    try:
        size = path.stat().st_size
        if size <= 0 or size > MAX_SOURCE_BYTES:
            raise WildcardImportError(
                f"source file for wildcard '{library_name}' has an invalid size"
            )
        raw = path.read_bytes()
        if len(raw) != size:
            raise WildcardImportError(
                f"source file for wildcard '{library_name}' changed while being read"
            )
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise WildcardImportError(
            f"source file for wildcard '{library_name}' is not valid UTF-8"
        ) from None
    if text.startswith("\ufeff"):
        raise WildcardImportError(
            f"source file for wildcard '{library_name}' must not contain a UTF-8 BOM"
        )
    return text.splitlines()


def _read_bounded(path: Path, maximum_bytes: int) -> bytes:
    size = path.stat().st_size
    if size <= 0 or size > maximum_bytes:
        raise WildcardImportError("wildcard import plan size is invalid")
    body = path.read_bytes()
    if len(body) != size:
        raise WildcardImportError("wildcard import plan changed while being read")
    return body


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError("non-finite JSON number")


if __name__ == "__main__":
    raise SystemExit(wildcard_import_main())
