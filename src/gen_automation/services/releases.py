from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    AuditEvent,
    IdempotencyRecord,
    Project,
    Release,
    ReleaseVersion,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.release_spec import ProjectCreate, ReleaseCreate
from gen_automation.schemas import ProjectRead, ReleaseRead
from gen_automation.services.wildcards import WildcardError, freeze_release_wildcards


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


@dataclass(frozen=True)
class IdempotentResult:
    response: ReleaseRead
    replayed: bool


async def create_project(
    session: AsyncSession,
    command: ProjectCreate,
    *,
    actor: str = "owner",
    correlation_id: str | None = None,
) -> ProjectRead:
    project = Project(slug=command.slug, name=command.name)
    session.add(project)
    await session.flush()
    session.add(
        AuditEvent(
            actor=actor,
            action="project.created",
            resource_type="project",
            resource_id=project.id,
            correlation_id=correlation_id or f"project-create:{project.id}",
            detail={"slug": project.slug},
            occurred_at=datetime.now(UTC),
        )
    )
    await session.commit()
    await session.refresh(project)
    return ProjectRead.model_validate(project)


async def create_release(
    session: AsyncSession,
    *,
    project_id: UUID,
    command: ReleaseCreate,
    idempotency_key: str,
    actor: str = "owner",
) -> IdempotentResult:
    scope = f"project:{project_id}:create-release"
    request_sha256 = canonical_sha256(
        {
            "project_id": str(project_id),
            "command": command.model_dump(mode="json"),
        }
    )

    existing = await session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_sha256 != request_sha256:
            raise ConflictError("idempotency key was already used for another request")
        return IdempotentResult(
            response=ReleaseRead.model_validate(existing.response_body),
            replayed=True,
        )

    project = await session.get(Project, project_id)
    if project is None:
        raise NotFoundError("project not found")

    duplicate_slug = await session.scalar(
        select(Release.id).where(
            Release.project_id == project_id,
            Release.slug == command.slug,
        )
    )
    if duplicate_slug is not None:
        raise ConflictError("release slug already exists in this project")

    try:
        wildcard_versions = await freeze_release_wildcards(
            session,
            command.specification,
        )
    except WildcardError as error:
        raise ConflictError(str(error)) from error
    frozen_specification = command.specification.model_copy(
        update={"wildcard_versions": list(wildcard_versions)}
    )

    now = datetime.now(UTC)
    release = Release(
        project_id=project_id,
        slug=command.slug,
        title=command.title,
        desired_accepted_count=command.desired_accepted_count,
    )
    session.add(release)
    await session.flush()

    specification_sha256 = canonical_sha256(frozen_specification)
    version = ReleaseVersion(
        release_id=release.id,
        version_no=1,
        specification=frozen_specification.model_dump(mode="json"),
        specification_sha256=specification_sha256,
        created_by=actor,
        created_at=now,
    )
    session.add(version)

    response = ReleaseRead(
        id=release.id,
        project_id=release.project_id,
        slug=release.slug,
        title=release.title,
        phase=release.phase,
        health=release.health,
        current_version_no=release.current_version_no,
        desired_accepted_count=release.desired_accepted_count,
        specification_sha256=specification_sha256,
        created_at=release.created_at or now,
        updated_at=release.updated_at or now,
    )

    session.add(
        AuditEvent(
            actor=actor,
            action="release.created",
            resource_type="release",
            resource_id=release.id,
            correlation_id=idempotency_key,
            detail={
                "project_id": str(project_id),
                "release_version": 1,
                "specification_sha256": specification_sha256,
                "wildcard_version_count": len(wildcard_versions),
            },
            occurred_at=now,
        )
    )
    session.add(
        IdempotencyRecord(
            scope=scope,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            response_status=201,
            response_body=response.model_dump(mode="json"),
            created_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    await session.commit()
    return IdempotentResult(response=response, replayed=False)


async def get_release(session: AsyncSession, release_id: UUID) -> ReleaseRead:
    row = (
        await session.execute(
            select(Release, ReleaseVersion)
            .join(
                ReleaseVersion,
                (ReleaseVersion.release_id == Release.id)
                & (ReleaseVersion.version_no == Release.current_version_no),
            )
            .where(Release.id == release_id)
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError("release not found")

    release, version = row
    return ReleaseRead(
        id=release.id,
        project_id=release.project_id,
        slug=release.slug,
        title=release.title,
        phase=release.phase,
        health=release.health,
        current_version_no=release.current_version_no,
        desired_accepted_count=release.desired_accepted_count,
        specification_sha256=version.specification_sha256,
        created_at=release.created_at,
        updated_at=release.updated_at,
    )
