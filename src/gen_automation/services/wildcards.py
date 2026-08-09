import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    AuditEvent,
    WildcardLibrary,
    WildcardLibraryVersion,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.release_spec import (
    GenerationParameters,
    ReleaseSpecification,
    WildcardVersionReference,
)
from gen_automation.domain.wildcards import (
    MAX_WILDCARD_EXPANSIONS,
    MAX_WILDCARD_LIBRARIES,
    MAX_WILDCARD_NESTING,
    MAX_WILDCARD_VERSIONS_PER_RELEASE,
    WildcardAppend,
    WildcardCreate,
    WildcardRead,
    WildcardReplace,
    validate_wildcard_entries,
)

WILDCARD_TOKEN = re.compile(r"__([a-z0-9]+(?:[._/-][a-z0-9]+)*)__")
MAX_RESOLVED_PROMPT_CHARACTERS = 20_000


class WildcardError(Exception):
    """Base error for operator-managed prompt wildcards."""


class WildcardInputError(WildcardError):
    pass


class WildcardNotFoundError(WildcardError):
    pass


class WildcardConflictError(WildcardError):
    pass


@dataclass(frozen=True, slots=True)
class FrozenWildcard:
    library_id: UUID
    version_id: UUID
    name: str
    version_no: int
    entries: tuple[str, ...]
    entries_sha256: str

    def reference(self) -> WildcardVersionReference:
        return WildcardVersionReference(
            name=self.name,
            library_id=self.library_id,
            version_id=self.version_id,
            version_no=self.version_no,
            entries_sha256=self.entries_sha256,
            entry_count=len(self.entries),
        )


@dataclass(frozen=True, slots=True)
class FrozenWildcardCatalog:
    by_name: Mapping[str, FrozenWildcard]

    @property
    def references(self) -> tuple[WildcardVersionReference, ...]:
        return tuple(self.by_name[name].reference() for name in sorted(self.by_name))


@dataclass(frozen=True, slots=True)
class ResolvedWildcardPrompts:
    prompt: str
    character_a_prompt: str
    character_b_prompt: str
    character_a_negative_prompt: str
    character_b_negative_prompt: str
    interaction_prompt: str
    camera_prompt: str
    negative_prompt: str
    detailer_prompt: str
    detailer_negative_prompt: str
    evidence: dict[str, object]


def extract_wildcard_names(text: str) -> tuple[str, ...]:
    return tuple(match.group(1) for match in WILDCARD_TOKEN.finditer(text))


def _validated_entries(raw: object) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise WildcardConflictError("stored wildcard entries are invalid")
    try:
        return validate_wildcard_entries(raw)
    except ValueError as error:
        raise WildcardConflictError("stored wildcard entries are invalid") from error


def _frozen(
    library: WildcardLibrary,
    version: WildcardLibraryVersion,
) -> FrozenWildcard:
    entries = _validated_entries(version.entries)
    entries_sha256 = canonical_sha256(entries)
    if (
        version.library_id != library.id
        or version.version_no < 1
        or version.entry_count != len(entries)
        or version.entries_sha256 != entries_sha256
    ):
        raise WildcardConflictError("stored wildcard version integrity check failed")
    return FrozenWildcard(
        library_id=library.id,
        version_id=version.id,
        name=library.name,
        version_no=version.version_no,
        entries=tuple(entries),
        entries_sha256=entries_sha256,
    )


def _read(
    library: WildcardLibrary,
    version: WildcardLibraryVersion,
) -> WildcardRead:
    frozen = _frozen(library, version)
    return WildcardRead(
        id=library.id,
        name=library.name,
        current_version_no=library.current_version_no,
        current_version_id=frozen.version_id,
        entries=list(frozen.entries),
        entries_sha256=frozen.entries_sha256,
        created_at=library.created_at,
        updated_at=library.updated_at,
    )


async def list_wildcard_libraries(session: AsyncSession) -> tuple[WildcardRead, ...]:
    rows = (
        await session.execute(
            select(WildcardLibrary, WildcardLibraryVersion)
            .join(
                WildcardLibraryVersion,
                (WildcardLibraryVersion.library_id == WildcardLibrary.id)
                & (WildcardLibraryVersion.version_no == WildcardLibrary.current_version_no),
            )
            .order_by(WildcardLibrary.name)
        )
    ).all()
    return tuple(_read(library, version) for library, version in rows)


async def get_wildcard_library(
    session: AsyncSession,
    *,
    name: str,
) -> WildcardRead:
    row = (
        await session.execute(
            select(WildcardLibrary, WildcardLibraryVersion)
            .join(
                WildcardLibraryVersion,
                (WildcardLibraryVersion.library_id == WildcardLibrary.id)
                & (WildcardLibraryVersion.version_no == WildcardLibrary.current_version_no),
            )
            .where(WildcardLibrary.name == name)
        )
    ).one_or_none()
    if row is None:
        raise WildcardNotFoundError("wildcard library not found")
    return _read(*row)


async def create_wildcard_library(
    session: AsyncSession,
    *,
    command: WildcardCreate,
    actor: str,
) -> WildcardRead:
    existing = await session.scalar(
        select(WildcardLibrary.id).where(WildcardLibrary.name == command.name)
    )
    if existing is not None:
        raise WildcardConflictError("wildcard library name already exists")
    total = int(await session.scalar(select(func.count()).select_from(WildcardLibrary)) or 0)
    if total >= MAX_WILDCARD_LIBRARIES:
        raise WildcardConflictError("wildcard library limit reached")

    now = datetime.now(UTC)
    entries = list(command.entries)
    library = WildcardLibrary(
        name=command.name,
        current_version_no=1,
        lock_version=1,
        created_by=actor,
        created_at=now,
        updated_at=now,
    )
    session.add(library)
    await session.flush()
    version = WildcardLibraryVersion(
        library_id=library.id,
        version_no=1,
        entries=entries,
        entry_count=len(entries),
        entries_sha256=canonical_sha256(entries),
        created_by=actor,
        created_at=now,
    )
    session.add(version)
    await session.flush()
    session.add(
        AuditEvent(
            actor=actor,
            action="wildcard_library.created",
            resource_type="wildcard_library",
            resource_id=library.id,
            correlation_id=f"wildcard-create:{library.id}",
            detail={
                "name": library.name,
                "version_no": 1,
                "entries_sha256": version.entries_sha256,
                "entry_count": len(entries),
            },
            occurred_at=now,
        )
    )
    await session.commit()
    return _read(library, version)


async def replace_wildcard_entries(
    session: AsyncSession,
    *,
    name: str,
    command: WildcardReplace,
    actor: str,
) -> WildcardRead:
    return await _write_next_version(
        session,
        name=name,
        expected_version_no=command.expected_version_no,
        requested_entries=list(command.entries),
        append=False,
        actor=actor,
    )


async def append_wildcard_entries(
    session: AsyncSession,
    *,
    name: str,
    command: WildcardAppend,
    actor: str,
) -> WildcardRead:
    return await _write_next_version(
        session,
        name=name,
        expected_version_no=command.expected_version_no,
        requested_entries=list(command.entries),
        append=True,
        actor=actor,
    )


async def _write_next_version(
    session: AsyncSession,
    *,
    name: str,
    expected_version_no: int,
    requested_entries: list[str],
    append: bool,
    actor: str,
) -> WildcardRead:
    library = await session.scalar(
        select(WildcardLibrary).where(WildcardLibrary.name == name).with_for_update()
    )
    if library is None:
        raise WildcardNotFoundError("wildcard library not found")
    if library.current_version_no != expected_version_no:
        raise WildcardConflictError("wildcard library changed; reload its current version")
    current = await session.scalar(
        select(WildcardLibraryVersion).where(
            WildcardLibraryVersion.library_id == library.id,
            WildcardLibraryVersion.version_no == library.current_version_no,
        )
    )
    if current is None:
        raise WildcardConflictError("wildcard library head is missing")
    current_entries = _validated_entries(current.entries)
    next_entries = current_entries + requested_entries if append else requested_entries
    try:
        validate_wildcard_entries(next_entries)
    except ValueError as error:
        raise WildcardInputError(str(error)) from error
    if next_entries == current_entries:
        return _read(library, current)

    now = datetime.now(UTC)
    next_version_no = library.current_version_no + 1
    version = WildcardLibraryVersion(
        library_id=library.id,
        version_no=next_version_no,
        entries=next_entries,
        entry_count=len(next_entries),
        entries_sha256=canonical_sha256(next_entries),
        created_by=actor,
        created_at=now,
    )
    session.add(version)
    library.current_version_no = next_version_no
    library.lock_version += 1
    library.updated_at = now
    await session.flush()
    session.add(
        AuditEvent(
            actor=actor,
            action=(
                "wildcard_library.entries_appended"
                if append
                else "wildcard_library.entries_replaced"
            ),
            resource_type="wildcard_library",
            resource_id=library.id,
            correlation_id=f"wildcard-version:{version.id}",
            detail={
                "name": library.name,
                "previous_version_no": expected_version_no,
                "version_no": next_version_no,
                "entries_sha256": version.entries_sha256,
                "entry_count": len(next_entries),
            },
            occurred_at=now,
        )
    )
    await session.commit()
    return _read(library, version)


async def freeze_release_wildcards(
    session: AsyncSession,
    specification: ReleaseSpecification,
) -> tuple[WildcardVersionReference, ...]:
    """Resolve current heads once and return the exact immutable release manifest."""

    provided = {reference.name: reference for reference in specification.wildcard_versions}
    initial_names = _generation_wildcard_names(specification)
    if not initial_names:
        if provided:
            raise WildcardInputError("release contains unused wildcard version references")
        return ()

    resolved: dict[str, FrozenWildcard] = {}
    pending = list(sorted(initial_names))
    while pending:
        name = pending.pop(0)
        if name in resolved:
            continue
        if len(resolved) >= MAX_WILDCARD_VERSIONS_PER_RELEASE:
            raise WildcardInputError("release uses too many wildcard libraries")
        frozen = await _load_requested_or_current(
            session,
            name=name,
            reference=provided.get(name),
        )
        resolved[name] = frozen
        for dependency in _entry_dependencies(frozen.entries):
            if dependency not in resolved and dependency not in pending:
                pending.append(dependency)

    unused = set(provided) - set(resolved)
    if unused:
        raise WildcardInputError("release contains unused wildcard version references")
    _validate_dependency_graph(resolved, initial_names=initial_names)
    return FrozenWildcardCatalog(resolved).references


async def load_frozen_wildcard_catalog(
    session: AsyncSession,
    specification: ReleaseSpecification,
) -> FrozenWildcardCatalog:
    references = specification.wildcard_versions
    initial_names = _generation_wildcard_names(specification)
    if not initial_names:
        if references:
            raise WildcardConflictError("frozen release has unused wildcard references")
        return FrozenWildcardCatalog({})
    if not references:
        raise WildcardConflictError("frozen release is missing wildcard version evidence")

    resolved: dict[str, FrozenWildcard] = {}
    for reference in references:
        frozen = await _load_exact_reference(session, reference)
        if frozen.name in resolved:
            raise WildcardConflictError("frozen release repeats a wildcard reference")
        resolved[frozen.name] = frozen
    _validate_dependency_graph(resolved, initial_names=initial_names)
    reachable = _reachable_names(resolved, initial_names)
    if reachable != set(resolved):
        raise WildcardConflictError("frozen release contains unused wildcard references")
    return FrozenWildcardCatalog(resolved)


async def _load_requested_or_current(
    session: AsyncSession,
    *,
    name: str,
    reference: WildcardVersionReference | None,
) -> FrozenWildcard:
    if reference is not None:
        return await _load_exact_reference(session, reference)
    row = (
        await session.execute(
            select(WildcardLibrary, WildcardLibraryVersion)
            .join(
                WildcardLibraryVersion,
                (WildcardLibraryVersion.library_id == WildcardLibrary.id)
                & (WildcardLibraryVersion.version_no == WildcardLibrary.current_version_no),
            )
            .where(WildcardLibrary.name == name)
        )
    ).one_or_none()
    if row is None:
        raise WildcardNotFoundError(f"wildcard library '{name}' was not found")
    return _frozen(*row)


async def _load_exact_reference(
    session: AsyncSession,
    reference: WildcardVersionReference,
) -> FrozenWildcard:
    row = (
        await session.execute(
            select(WildcardLibrary, WildcardLibraryVersion)
            .join(
                WildcardLibraryVersion,
                WildcardLibraryVersion.library_id == WildcardLibrary.id,
            )
            .where(
                WildcardLibrary.id == reference.library_id,
                WildcardLibrary.name == reference.name,
                WildcardLibraryVersion.id == reference.version_id,
                WildcardLibraryVersion.version_no == reference.version_no,
            )
        )
    ).one_or_none()
    if row is None:
        raise WildcardConflictError(f"frozen wildcard version '{reference.name}' is unavailable")
    frozen = _frozen(*row)
    if (
        frozen.entries_sha256 != reference.entries_sha256
        or len(frozen.entries) != reference.entry_count
    ):
        raise WildcardConflictError(
            f"frozen wildcard version '{reference.name}' failed its integrity check"
        )
    return frozen


def _entry_dependencies(entries: Iterable[str]) -> set[str]:
    dependencies: set[str] = set()
    for entry in entries:
        dependencies.update(extract_wildcard_names(entry))
    return dependencies


def _validate_dependency_graph(
    catalog: Mapping[str, FrozenWildcard],
    *,
    initial_names: set[str],
) -> None:
    missing: set[str] = set()
    graph: dict[str, set[str]] = {}
    for name, wildcard in catalog.items():
        dependencies = _entry_dependencies(wildcard.entries)
        graph[name] = dependencies
        missing.update(dependencies - set(catalog))
    missing.update(initial_names - set(catalog))
    if missing:
        first = sorted(missing)[0]
        raise WildcardConflictError(f"wildcard library '{first}' is missing")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str, depth: int) -> None:
        if name in visiting:
            raise WildcardConflictError(f"wildcard dependency cycle includes '{name}'")
        if depth > MAX_WILDCARD_NESTING:
            raise WildcardConflictError("wildcard nesting limit exceeded")
        if name in visited:
            return
        visiting.add(name)
        for dependency in sorted(graph[name]):
            visit(dependency, depth + 1)
        visiting.remove(name)
        visited.add(name)

    for root in sorted(initial_names):
        visit(root, 1)


def _reachable_names(
    catalog: Mapping[str, FrozenWildcard],
    initial_names: set[str],
) -> set[str]:
    reached: set[str] = set()
    pending = list(initial_names)
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        pending.extend(_entry_dependencies(catalog[name].entries) - reached)
    return reached


def resolve_wildcard_prompts(
    specification: ReleaseSpecification,
    catalog: FrozenWildcardCatalog,
    *,
    seed: int,
    generation: GenerationParameters | None = None,
) -> ResolvedWildcardPrompts:
    selected_generation = generation or specification.generation
    selections: list[dict[str, object]] = []
    prompt = _expand_text(
        selected_generation.prompt,
        catalog=catalog.by_name,
        seed=seed,
        field_name="prompt",
        selections=selections,
    )
    character_a_prompt = _expand_text(
        selected_generation.character_a_prompt,
        catalog=catalog.by_name,
        seed=seed,
        field_name="character_a_prompt",
        selections=selections,
    )
    character_b_prompt = _expand_text(
        selected_generation.character_b_prompt,
        catalog=catalog.by_name,
        seed=seed,
        field_name="character_b_prompt",
        selections=selections,
    )
    character_a_negative_prompt = _expand_text(
        selected_generation.character_a_negative_prompt,
        catalog=catalog.by_name,
        seed=seed,
        field_name="character_a_negative_prompt",
        selections=selections,
    )
    character_b_negative_prompt = _expand_text(
        selected_generation.character_b_negative_prompt,
        catalog=catalog.by_name,
        seed=seed,
        field_name="character_b_negative_prompt",
        selections=selections,
    )
    interaction_prompt = _expand_text(
        selected_generation.interaction_prompt,
        catalog=catalog.by_name,
        seed=seed,
        field_name="interaction_prompt",
        selections=selections,
    )
    camera_prompt = _expand_text(
        selected_generation.camera_prompt,
        catalog=catalog.by_name,
        seed=seed,
        field_name="camera_prompt",
        selections=selections,
    )
    negative_prompt = _expand_text(
        selected_generation.negative_prompt,
        catalog=catalog.by_name,
        seed=seed,
        field_name="negative_prompt",
        selections=selections,
    )
    detailer_prompt = _expand_text(
        selected_generation.detailer_prompt,
        catalog=catalog.by_name,
        seed=seed,
        field_name="detailer_prompt",
        selections=selections,
    )
    detailer_negative_prompt = _expand_text(
        selected_generation.detailer_negative_prompt,
        catalog=catalog.by_name,
        seed=seed,
        field_name="detailer_negative_prompt",
        selections=selections,
    )
    references = [reference.model_dump(mode="json") for reference in catalog.references]
    evidence: dict[str, object] = {
        "schema_version": 3 if selected_generation.duo_contract_version == 2 else 2,
        "seed": seed,
        "source_prompt": selected_generation.prompt,
        "source_character_a_prompt": selected_generation.character_a_prompt,
        "source_character_b_prompt": selected_generation.character_b_prompt,
        "source_negative_prompt": selected_generation.negative_prompt,
        "source_detailer_prompt": selected_generation.detailer_prompt,
        "source_detailer_negative_prompt": selected_generation.detailer_negative_prompt,
        "source_prompt_sha256": _text_sha256(selected_generation.prompt),
        "source_character_a_prompt_sha256": _text_sha256(selected_generation.character_a_prompt),
        "source_character_b_prompt_sha256": _text_sha256(selected_generation.character_b_prompt),
        "source_negative_prompt_sha256": _text_sha256(selected_generation.negative_prompt),
        "source_detailer_prompt_sha256": _text_sha256(selected_generation.detailer_prompt),
        "source_detailer_negative_prompt_sha256": _text_sha256(
            selected_generation.detailer_negative_prompt
        ),
        "resolved_prompt_sha256": _text_sha256(prompt),
        "resolved_character_a_prompt_sha256": _text_sha256(character_a_prompt),
        "resolved_character_b_prompt_sha256": _text_sha256(character_b_prompt),
        "resolved_negative_prompt_sha256": _text_sha256(negative_prompt),
        "resolved_detailer_prompt_sha256": _text_sha256(detailer_prompt),
        "resolved_detailer_negative_prompt_sha256": _text_sha256(detailer_negative_prompt),
        "wildcard_versions": references,
        "selections": selections,
    }
    if selected_generation.duo_contract_version == 2:
        evidence.update(
            {
                "source_character_a_negative_prompt": (
                    selected_generation.character_a_negative_prompt
                ),
                "source_character_b_negative_prompt": (
                    selected_generation.character_b_negative_prompt
                ),
                "source_interaction_prompt": selected_generation.interaction_prompt,
                "source_camera_prompt": selected_generation.camera_prompt,
                "source_character_a_negative_prompt_sha256": _text_sha256(
                    selected_generation.character_a_negative_prompt
                ),
                "source_character_b_negative_prompt_sha256": _text_sha256(
                    selected_generation.character_b_negative_prompt
                ),
                "source_interaction_prompt_sha256": _text_sha256(
                    selected_generation.interaction_prompt
                ),
                "source_camera_prompt_sha256": _text_sha256(selected_generation.camera_prompt),
                "resolved_character_a_negative_prompt_sha256": _text_sha256(
                    character_a_negative_prompt
                ),
                "resolved_character_b_negative_prompt_sha256": _text_sha256(
                    character_b_negative_prompt
                ),
                "resolved_interaction_prompt_sha256": _text_sha256(interaction_prompt),
                "resolved_camera_prompt_sha256": _text_sha256(camera_prompt),
            }
        )
    return ResolvedWildcardPrompts(
        prompt=prompt,
        character_a_prompt=character_a_prompt,
        character_b_prompt=character_b_prompt,
        character_a_negative_prompt=character_a_negative_prompt,
        character_b_negative_prompt=character_b_negative_prompt,
        interaction_prompt=interaction_prompt,
        camera_prompt=camera_prompt,
        negative_prompt=negative_prompt,
        detailer_prompt=detailer_prompt,
        detailer_negative_prompt=detailer_negative_prompt,
        evidence=evidence,
    )


def _generation_wildcard_names(specification: ReleaseSpecification) -> set[str]:
    names: set[str] = set()
    for batch in specification.ordered_generation_batches:
        generation = batch.generation
        for text in (
            generation.prompt,
            generation.character_a_prompt,
            generation.character_b_prompt,
            generation.character_a_negative_prompt,
            generation.character_b_negative_prompt,
            generation.interaction_prompt,
            generation.camera_prompt,
            generation.negative_prompt,
            generation.detailer_prompt,
            generation.detailer_negative_prompt,
        ):
            names.update(extract_wildcard_names(text))
    return names


def _expand_text(
    text: str,
    *,
    catalog: Mapping[str, FrozenWildcard],
    seed: int,
    field_name: str,
    selections: list[dict[str, object]],
) -> str:
    occurrence = [0]

    def expand(value: str, *, depth: int, stack: tuple[str, ...]) -> str:
        if depth > MAX_WILDCARD_NESTING:
            raise WildcardConflictError("wildcard nesting limit exceeded")

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            wildcard = catalog.get(name)
            if wildcard is None:
                raise WildcardConflictError(f"wildcard library '{name}' is missing")
            if name in stack:
                raise WildcardConflictError(f"wildcard dependency cycle includes '{name}'")
            if len(selections) >= MAX_WILDCARD_EXPANSIONS:
                raise WildcardConflictError("wildcard expansion limit exceeded")
            occurrence[0] += 1
            digest = canonical_sha256(
                {
                    "schema": "wildcard-selection/v1",
                    "seed": seed,
                    "field": field_name,
                    "occurrence": occurrence[0],
                    "name": wildcard.name,
                    "version_id": str(wildcard.version_id),
                    "entries_sha256": wildcard.entries_sha256,
                }
            )
            index = int(digest[:16], 16) % len(wildcard.entries)
            entry = wildcard.entries[index]
            selections.append(
                {
                    "field": field_name,
                    "occurrence": occurrence[0],
                    "depth": depth,
                    "name": wildcard.name,
                    "version_id": str(wildcard.version_id),
                    "version_no": wildcard.version_no,
                    "entries_sha256": wildcard.entries_sha256,
                    "entry_index": index,
                    "entry_sha256": _text_sha256(entry),
                }
            )
            return expand(entry, depth=depth + 1, stack=(*stack, name))

        expanded = WILDCARD_TOKEN.sub(replace, value)
        if len(expanded) > MAX_RESOLVED_PROMPT_CHARACTERS:
            raise WildcardConflictError("resolved prompt exceeds the character limit")
        return expanded

    return expand(text, depth=1, stack=())


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
