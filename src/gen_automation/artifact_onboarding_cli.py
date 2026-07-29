import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

from pydantic import SecretStr, ValidationError

from gen_automation.artifact_onboarding_settings import ArtifactOnboardingSettings
from gen_automation.db.session import Database
from gen_automation.domain.artifact_onboarding import ArtifactOnboardingPlan
from gen_automation.domain.canonical import canonical_json_bytes
from gen_automation.services.artifact_onboarding import (
    MAX_PLAN_BYTES,
    ArtifactOnboardingError,
    ArtifactOnboardingResult,
    onboard_artifacts,
    parse_onboarding_plan,
)
from gen_automation.services.compliance_registry import ComplianceRegistryError
from gen_automation.storage.base import ObjectStore, ObjectStoreError
from gen_automation.storage.s3 import S3ObjectStore


def artifact_onboarding_main() -> int:
    """Run one bounded onboarding plan without accepting any secret arguments."""

    parser = argparse.ArgumentParser(
        prog="gen-automation-artifacts",
        description="Validate and onboard worker artifacts and ComfyUI workflows.",
    )
    parser.add_argument("plan", type=Path, help="Non-secret onboarding plan JSON.")
    parser.add_argument(
        "--manifest-out",
        required=True,
        type=Path,
        help="Destination for canonical worker ArtifactManifest JSON.",
    )
    parser.add_argument(
        "--sha256-out",
        type=Path,
        help="Optional destination for the separate manifest trust-anchor SHA-256.",
    )
    arguments = parser.parse_args()

    try:
        plan_path = arguments.plan.resolve(strict=True)
        plan = parse_onboarding_plan(_read_bounded(plan_path, MAX_PLAN_BYTES))
        settings = ArtifactOnboardingSettings()
        manifest_path = arguments.manifest_out.resolve(strict=False)
        sha256_path = (
            arguments.sha256_out.resolve(strict=False)
            if arguments.sha256_out is not None
            else manifest_path.with_name(f"{manifest_path.name}.sha256")
        )
        _validate_output_paths(
            plan_path=plan_path,
            plan=plan,
            plan_directory=plan_path.parent,
            manifest_path=manifest_path,
            sha256_path=sha256_path,
        )
        result = asyncio.run(
            _apply_plan(
                settings=settings,
                plan=plan,
                plan_directory=plan_path.parent,
            )
        )
        _write_private_atomic(
            manifest_path,
            canonical_json_bytes(result.manifest) + b"\n",
        )
        _write_private_atomic(
            sha256_path,
            f"{result.manifest.manifest_sha256}\n".encode("ascii"),
        )
    except (
        ArtifactOnboardingError,
        ComplianceRegistryError,
        ObjectStoreError,
    ) as error:
        print(f"Artifact onboarding failed: {error}", file=sys.stderr)
        return 1
    except ValidationError:
        print(
            "Artifact onboarding configuration is invalid. Supply database and "
            "object-store settings through the job environment/secret identity.",
            file=sys.stderr,
        )
        return 2
    except (OSError, RuntimeError):
        print(
            "Artifact onboarding failed while reading or writing a local file.",
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            "Artifact onboarding failed without exposing database or secret details. "
            "Check restricted service logs.",
            file=sys.stderr,
        )
        return 1

    print(
        f"Onboarded {len(result.manifest.artifacts)} worker artifacts and "
        f"{len(result.workflows)} workflows."
    )
    print(f"Manifest SHA-256: {result.manifest.manifest_sha256}")
    print(f"Manifest JSON: {manifest_path}")
    print(f"Trust anchor: {sha256_path}")
    return 0


async def _apply_plan(
    *,
    settings: ArtifactOnboardingSettings,
    plan: ArtifactOnboardingPlan,
    plan_directory: Path,
) -> ArtifactOnboardingResult:
    database = Database(settings.database_url)
    workflow_store = _workflow_store(settings)
    artifact_store = _artifact_store(settings)
    try:
        await workflow_store.ping()
        await artifact_store.ping()
        async with database.sessions() as session:
            result: ArtifactOnboardingResult = await onboard_artifacts(
                session,
                plan=plan,
                plan_directory=plan_directory,
                artifact_store=artifact_store,
                workflow_store=workflow_store,
            )
            return result
    finally:
        try:
            await workflow_store.close()
        finally:
            try:
                await artifact_store.close()
            finally:
                await database.dispose()


def _validate_output_paths(
    *,
    plan_path: Path,
    plan: ArtifactOnboardingPlan,
    plan_directory: Path,
    manifest_path: Path,
    sha256_path: Path,
) -> None:
    protected_paths = {plan_path}
    for value in (
        *(entry.local_path for entry in plan.artifacts if entry.local_path is not None),
        *(entry.local_path for entry in plan.workflows),
    ):
        local_path = Path(value)
        if not local_path.is_absolute():
            local_path = plan_directory / local_path
        protected_paths.add(local_path.resolve(strict=True))
    if manifest_path == sha256_path:
        raise ArtifactOnboardingError("manifest and trust-anchor outputs must be different files")
    if manifest_path in protected_paths or sha256_path in protected_paths:
        raise ArtifactOnboardingError("output paths must not overwrite onboarding inputs")


def _workflow_store(settings: ArtifactOnboardingSettings) -> ObjectStore:
    return S3ObjectStore(
        bucket=settings.storage_bucket,
        region=settings.storage_region,
        endpoint_url=(
            str(settings.storage_endpoint_url)
            if settings.storage_endpoint_url is not None
            else None
        ),
        access_key_id=_secret_value(settings.storage_access_key_id),
        secret_access_key=_secret_value(settings.storage_secret_access_key),
        session_token=_secret_value(settings.storage_session_token),
    )


def _artifact_store(settings: ArtifactOnboardingSettings) -> ObjectStore:
    return S3ObjectStore(
        bucket=settings.salad_worker_artifact_bucket,
        region=settings.salad_worker_artifact_region,
        endpoint_url=(
            str(settings.salad_worker_artifact_endpoint_url)
            if settings.salad_worker_artifact_endpoint_url is not None
            else None
        ),
        access_key_id=_secret_value(settings.salad_worker_artifact_access_key_id),
        secret_access_key=_secret_value(settings.salad_worker_artifact_secret_access_key),
        session_token=_secret_value(settings.salad_worker_artifact_session_token),
    )


def _secret_value(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


def _read_bounded(path: Path, max_bytes: int) -> bytes:
    with path.open("rb") as file_object:
        body = file_object.read(max_bytes + 1)
    if not body or len(body) > max_bytes:
        raise ArtifactOnboardingError("onboarding plan size is invalid")
    return body


def _write_private_atomic(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        os.chmod(temporary_path, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as file_object:
            descriptor = -1
            file_object.write(body)
            file_object.flush()
            os.fsync(file_object.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(artifact_onboarding_main())
