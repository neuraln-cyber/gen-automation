"""Bounded, queue-preserving rollout of the reviewed I2V LoRA worker profile."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gen_automation.config import Settings
from gen_automation.db.models import I2VAttempt, I2VJob
from gen_automation.domain.i2v import I2VAttemptState, I2VJobState
from gen_automation.domain.i2v_loras import I2VLoraSettingsKind, classify_i2v_lora_settings
from gen_automation.i2v_worker.manifest_contract import validated_i2v_manifest_objects
from gen_automation.integrations.salad import SALAD_QUEUE_JOB_PAGE_SIZE
from gen_automation.integrations.salad.models import (
    JSONObject,
    JSONValue,
    SaladContainerGroup,
    SaladContainerGroupInstance,
    SaladJobStatus,
)
from gen_automation.services.i2v_environment import (
    I2VRuntimeEnvironment,
    i2v_runtime_config_from_settings,
    i2v_worker_identity,
)
from gen_automation.services.i2v_salad import (
    I2V_SALAD_GPU_CLASS_NAME,
    I2VSaladClient,
    I2VSaladConfig,
    I2VSaladError,
    validate_i2v_group_contract,
)
from gen_automation.services.runtime_secrets import RuntimeSecretResolver

REVIEWED_MANIFEST_BUCKET = "gen-automation-staging-861912887470-eu-central-1-models"
REVIEWED_MANIFEST_KEY = (
    "worker/i2v/manifests/sha256/"
    "f0cd579606c8bc7fbf77ee8353b5c542395576d08f21e9acea37a1e2de19876e.json"
)
REVIEWED_MANIFEST_VERSION = "u4bSnCPzDJ4zctrA2Nr66ji0Zh2qPpXX"
REVIEWED_MANIFEST_BYTES = 6_153
REVIEWED_SOURCE_SHA256 = "f0cd579606c8bc7fbf77ee8353b5c542395576d08f21e9acea37a1e2de19876e"
REVIEWED_CANONICAL_MANIFEST_SHA256 = (
    "ebdeca736ee3e9ea4e4b7118c9e4b54dfcfd1bbde5a761f424aa85b1670b806f"
)
REVIEWED_MODEL_OBJECTS_SHA256 = "be5802ffc52ee6bfa6c64a135dfdef37e4e0274e4098c9eb87e4edaafc4719a6"
REVIEWED_ARTIFACT_IDENTITY_SHA256 = (
    "68f6c28831ac2a8e1801ba420c9816a29e09c8cc4738aae85611955553a3d301"
)
REVIEWED_PROVIDER_GROUP_ID = "411e67d9-1584-4c60-8a6e-301319a64ea3"

_QUEUE_LOCK_KEY = 749220037
_ACTIVE_JOB_STATES = (
    I2VJobState.CLAIMED,
    I2VJobState.RUNNING,
    I2VJobState.CANCEL_REQUESTED,
)
_ACTIVE_ATTEMPT_STATES = (I2VAttemptState.CREATED, I2VAttemptState.RUNNING)
_ACTIVE_PROVIDER_STATES = (SaladJobStatus.PENDING, SaladJobStatus.RUNNING)
_MAX_PROVIDER_JOB_PAGES = 100
_WORKER_CREDENTIAL_KEYS = frozenset(
    {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
)
_HOST_PATCH_KEYS = (
    "GEN_AUTOMATION_I2V_ENABLED",
    "GEN_AUTOMATION_I2V_HIRES_PROFILE_ENABLED",
    "GEN_AUTOMATION_I2V_WORKER_IMAGE",
    "GEN_AUTOMATION_I2V_WORKER_SOURCE_REVISION",
    "GEN_AUTOMATION_I2V_PRIVATE_MANIFEST_SOURCE_SHA256",
    "GEN_AUTOMATION_I2V_MODEL_MANIFEST_JSON",
    "GEN_AUTOMATION_I2V_MODEL_MANIFEST_SHA256",
    "GEN_AUTOMATION_I2V_LORA_WORKER_ENABLED",
    "GEN_AUTOMATION_I2V_LORA_PROFILE_ENABLED",
)


class I2VLoraRolloutError(RuntimeError):
    """A redacted rollout failure safe to print in an operator workflow."""


class VersionedManifestClient(Protocol):
    def get_object(self, **kwargs: object) -> Mapping[str, object]: ...


class VersionedArtifactClient(Protocol):
    def head_object(self, **kwargs: object) -> Mapping[str, object]: ...

    def close(self) -> None: ...


type ArtifactClientFactory = Callable[[Mapping[str, str]], VersionedArtifactClient]


@dataclass(frozen=True, slots=True)
class ReviewedManifestCoordinates:
    bucket: str
    key: str
    version_id: str
    source_sha256: str

    def validate_exact(self) -> None:
        expected = (
            REVIEWED_MANIFEST_BUCKET,
            REVIEWED_MANIFEST_KEY,
            REVIEWED_MANIFEST_VERSION,
            REVIEWED_SOURCE_SHA256,
        )
        if (self.bucket, self.key, self.version_id, self.source_sha256) != expected:
            raise I2VLoraRolloutError("private manifest coordinates are not the reviewed version")


@dataclass(frozen=True, slots=True)
class PreparedReviewedManifest:
    canonical_json: str
    canonical_sha256: str
    objects: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class ProviderRollbackState:
    schema: str
    group_id: str
    group_name: str
    prior_image: str
    prior_readiness_probe: JSONObject | None
    prior_environment_keys: tuple[str, ...]
    prior_environment_identity_sha256: str
    prior_contract: JSONObject
    prior_version: int
    promoted_image: str
    promoted_readiness_probe: JSONObject
    promoted_environment_keys: tuple[str, ...]
    promoted_environment_identity_sha256: str
    promoted_version: int

    @classmethod
    def from_json(cls, raw: object) -> ProviderRollbackState:
        if not isinstance(raw, dict) or raw.get("schema") != "gen-automation/i2v-lora-rollback/v2":
            raise I2VLoraRolloutError("provider rollback state is invalid")
        try:
            return cls(
                schema=cast(str, raw["schema"]),
                group_id=cast(str, raw["group_id"]),
                group_name=cast(str, raw["group_name"]),
                prior_image=cast(str, raw["prior_image"]),
                prior_readiness_probe=cast(JSONObject | None, raw["prior_readiness_probe"]),
                prior_environment_keys=tuple(cast(list[str], raw["prior_environment_keys"])),
                prior_environment_identity_sha256=cast(
                    str, raw["prior_environment_identity_sha256"]
                ),
                prior_contract=cast(JSONObject, raw["prior_contract"]),
                prior_version=cast(int, raw["prior_version"]),
                promoted_image=cast(str, raw["promoted_image"]),
                promoted_readiness_probe=cast(JSONObject, raw["promoted_readiness_probe"]),
                promoted_environment_keys=tuple(cast(list[str], raw["promoted_environment_keys"])),
                promoted_environment_identity_sha256=cast(
                    str, raw["promoted_environment_identity_sha256"]
                ),
                promoted_version=cast(int, raw["promoted_version"]),
            )
        except (KeyError, TypeError):
            raise I2VLoraRolloutError("provider rollback state is invalid") from None


@dataclass(frozen=True, slots=True)
class I2VLoraRolloutResult:
    operation: str
    provider_image: str
    provider_ready: bool
    durable_active_jobs: int
    durable_active_attempts: int
    provider_active_jobs: int


async def rollout_status(
    *,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    salad_client: I2VSaladClient,
) -> I2VLoraRolloutResult:
    """Read durable and provider state without taking a lock or mutating anything."""

    async with sessions() as session:
        active_jobs = int(
            await session.scalar(
                select(func.count()).select_from(I2VJob).where(I2VJob.state.in_(_ACTIVE_JOB_STATES))
            )
            or 0
        )
        active_attempts = int(
            await session.scalar(
                select(func.count())
                .select_from(I2VAttempt)
                .where(I2VAttempt.state.in_(_ACTIVE_ATTEMPT_STATES))
            )
            or 0
        )
    group_name = settings.i2v_salad_container_group_name
    group = await salad_client.get_container_group(group_name)
    provider_jobs = await _provider_active_jobs(
        salad_client,
        queue_name=settings.i2v_salad_queue_name,
    )
    instances = await salad_client.list_container_group_instances(group_name)
    return I2VLoraRolloutResult(
        operation="status",
        provider_image=_group_image(group),
        provider_ready=_has_exact_ready_instance(group, instances),
        durable_active_jobs=active_jobs,
        durable_active_attempts=active_attempts,
        provider_active_jobs=len(provider_jobs),
    )


async def fetch_reviewed_manifest(
    client: VersionedManifestClient,
    coordinates: ReviewedManifestCoordinates,
) -> PreparedReviewedManifest:
    """Fetch exactly one immutable S3 version and verify all reviewed identities."""

    coordinates.validate_exact()

    def read() -> tuple[bytes, Mapping[str, object]]:
        response = client.get_object(
            Bucket=coordinates.bucket,
            Key=coordinates.key,
            VersionId=coordinates.version_id,
        )
        body = response.get("Body")
        if body is None or not callable(getattr(body, "read", None)):
            raise I2VLoraRolloutError("private manifest response has no readable body")
        payload = cast(Any, body).read(REVIEWED_MANIFEST_BYTES + 1)
        if not isinstance(payload, bytes):
            raise I2VLoraRolloutError("private manifest response body is invalid")
        return payload, response

    payload, response = await asyncio.to_thread(read)
    if response.get("VersionId") != coordinates.version_id:
        raise I2VLoraRolloutError("private manifest response version changed")
    if response.get("ContentLength") not in {None, REVIEWED_MANIFEST_BYTES}:
        raise I2VLoraRolloutError("private manifest response length changed")
    if len(payload) != REVIEWED_MANIFEST_BYTES:
        raise I2VLoraRolloutError("private manifest byte length changed")
    if hashlib.sha256(payload).hexdigest() != REVIEWED_SOURCE_SHA256:
        raise I2VLoraRolloutError("private manifest source digest changed")
    try:
        document = json.loads(payload)
        validated = validated_i2v_manifest_objects(document, reviewed_loras_enabled=True)
    except (UnicodeDecodeError, ValueError):
        raise I2VLoraRolloutError("private manifest contract is invalid") from None
    canonical = json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    canonical_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if canonical_sha256 != REVIEWED_CANONICAL_MANIFEST_SHA256:
        raise I2VLoraRolloutError("canonical private manifest digest changed")
    return PreparedReviewedManifest(
        canonical_json=canonical,
        canonical_sha256=canonical_sha256,
        objects=tuple(cast(Mapping[str, object], validated[role]) for role in validated),
    )


async def verify_reviewed_artifact_access(
    client: VersionedArtifactClient,
    manifest: PreparedReviewedManifest,
) -> None:
    """Prove the exact worker role can read every immutable model object."""

    if len(manifest.objects) != 14:
        raise I2VLoraRolloutError("reviewed model object inventory is incomplete")

    def verify() -> None:
        for value in manifest.objects:
            key = value.get("key")
            version_id = value.get("version_id")
            byte_size = value.get("bytes")
            if (
                not isinstance(key, str)
                or not isinstance(version_id, str)
                or not isinstance(byte_size, int)
                or isinstance(byte_size, bool)
            ):
                raise I2VLoraRolloutError("reviewed model object identity is invalid")
            try:
                response = client.head_object(
                    Bucket=REVIEWED_MANIFEST_BUCKET,
                    Key=key,
                    VersionId=version_id,
                )
            except Exception:
                raise I2VLoraRolloutError(
                    "the worker artifact role cannot read every reviewed object"
                ) from None
            if (
                response.get("VersionId") != version_id
                or response.get("ContentLength") != byte_size
            ):
                raise I2VLoraRolloutError("reviewed model object readback changed")

    await asyncio.to_thread(verify)


def reviewed_target_settings(
    settings: Settings,
    *,
    worker_image: str,
    worker_source_revision: str,
    manifest: PreparedReviewedManifest,
) -> Settings:
    values = settings.model_dump()
    values.update(
        {
            "i2v_enabled": True,
            "i2v_hires_profile_enabled": True,
            "i2v_lora_worker_enabled": True,
            "i2v_lora_profile_enabled": False,
            "i2v_worker_image": worker_image,
            "i2v_worker_source_revision": worker_source_revision,
            "i2v_private_manifest_source_sha256": REVIEWED_SOURCE_SHA256,
            "i2v_model_manifest_json": SecretStr(manifest.canonical_json),
            "i2v_model_manifest_sha256": SecretStr(manifest.canonical_sha256),
        }
    )
    try:
        return Settings.model_validate(values)
    except ValueError:
        raise I2VLoraRolloutError("reviewed target configuration is invalid") from None


async def resolve_reviewed_worker_environment(
    settings: Settings,
    resolver: RuntimeSecretResolver,
) -> Mapping[str, str]:
    environment = dict(await I2VRuntimeEnvironment(settings=settings, resolver=resolver).resolve())
    _validate_worker_environment_identity(environment, settings=settings)
    return environment


def reviewed_host_patch(settings: Settings) -> Mapping[str, str]:
    manifest_json = settings.i2v_model_manifest_json
    manifest_sha256 = settings.i2v_model_manifest_sha256
    if manifest_json is None or manifest_sha256 is None:
        raise I2VLoraRolloutError("reviewed host manifest is unavailable")
    patch = {
        "GEN_AUTOMATION_I2V_ENABLED": "true",
        "GEN_AUTOMATION_I2V_HIRES_PROFILE_ENABLED": "true",
        "GEN_AUTOMATION_I2V_WORKER_IMAGE": settings.i2v_worker_image or "",
        "GEN_AUTOMATION_I2V_WORKER_SOURCE_REVISION": settings.i2v_worker_source_revision or "",
        "GEN_AUTOMATION_I2V_PRIVATE_MANIFEST_SOURCE_SHA256": (
            settings.i2v_private_manifest_source_sha256 or ""
        ),
        "GEN_AUTOMATION_I2V_MODEL_MANIFEST_JSON": manifest_json.get_secret_value(),
        "GEN_AUTOMATION_I2V_MODEL_MANIFEST_SHA256": manifest_sha256.get_secret_value(),
        "GEN_AUTOMATION_I2V_LORA_WORKER_ENABLED": "true",
        "GEN_AUTOMATION_I2V_LORA_PROFILE_ENABLED": "false",
    }
    if tuple(patch) != _HOST_PATCH_KEYS or any(not value for value in patch.values()):
        raise I2VLoraRolloutError("reviewed host patch is incomplete")
    return patch


def write_private_json(path: Path, value: object) -> None:
    _atomic_private_write(
        path,
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n",
    )


def write_private_host_patch(path: Path, values: Mapping[str, str]) -> None:
    if tuple(values) != _HOST_PATCH_KEYS or any(
        "\n" in value or "\r" in value for value in values.values()
    ):
        raise I2VLoraRolloutError("reviewed host patch is invalid")
    _atomic_private_write(path, "".join(f"{key}={value}\n" for key, value in values.items()))


def read_provider_rollback_state(path: Path) -> ProviderRollbackState:
    try:
        return ProviderRollbackState.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        raise I2VLoraRolloutError("provider rollback state could not be read") from None


async def assert_zero_active_rollout_work(
    session: AsyncSession,
    client: I2VSaladClient,
    *,
    queue_name: str,
) -> tuple[int, int, int]:
    await _lock_i2v_queue(session)
    active_jobs = int(
        await session.scalar(
            select(func.count()).select_from(I2VJob).where(I2VJob.state.in_(_ACTIVE_JOB_STATES))
        )
        or 0
    )
    active_attempts = int(
        await session.scalar(
            select(func.count())
            .select_from(I2VAttempt)
            .where(I2VAttempt.state.in_(_ACTIVE_ATTEMPT_STATES))
        )
        or 0
    )
    provider_jobs = await _provider_active_jobs(client, queue_name=queue_name)
    queue = await client.get_queue(queue_name)
    if active_jobs or active_attempts or provider_jobs or queue.current_queue_length:
        raise I2VLoraRolloutError(
            "I2V rollout requires zero active durable jobs, attempts, and provider queue jobs"
        )
    return active_jobs, active_attempts, len(provider_jobs)


async def dry_run_reviewed_worker_rollout(
    *,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    salad_client: I2VSaladClient,
    resolver: RuntimeSecretResolver,
    manifest_client: VersionedManifestClient,
    artifact_client_factory: ArtifactClientFactory,
    coordinates: ReviewedManifestCoordinates,
    worker_image: str,
    worker_source_revision: str,
    prepared_host_env_output: Path,
) -> I2VLoraRolloutResult:
    manifest = await fetch_reviewed_manifest(manifest_client, coordinates)
    target = reviewed_target_settings(
        settings,
        worker_image=worker_image,
        worker_source_revision=worker_source_revision,
        manifest=manifest,
    )
    environment = await resolve_reviewed_worker_environment(target, resolver)
    artifact_client = artifact_client_factory(environment)
    try:
        await verify_reviewed_artifact_access(artifact_client, manifest)
    finally:
        artifact_client.close()
    write_private_host_patch(prepared_host_env_output, reviewed_host_patch(target))
    async with sessions() as session, session.begin():
        counts = await assert_zero_active_rollout_work(
            session,
            salad_client,
            queue_name=target.i2v_salad_queue_name,
        )
        group = await salad_client.get_container_group(target.i2v_salad_container_group_name)
    if group.pending_change:
        raise I2VLoraRolloutError("provider group already has a pending change")
    prior_config = i2v_runtime_config_from_settings(
        settings.model_copy(update={"i2v_enabled": True})
    ).salad
    _validate_prior_group_contract(group, prior_config)
    gpu_classes = await salad_client.list_gpu_classes()
    if not any(
        item.id == prior_config.gpu_class_id and item.name == I2V_SALAD_GPU_CLASS_NAME
        for item in gpu_classes
    ):
        raise I2VLoraRolloutError("provider GPU class is not the exact reviewed RTX 5090")
    instances = await salad_client.list_container_group_instances(
        target.i2v_salad_container_group_name
    )
    if not _has_exact_ready_instance(group, instances):
        raise I2VLoraRolloutError("provider group is not stable and ready before rollout")
    return I2VLoraRolloutResult(
        operation="dry-run",
        provider_image=_group_image(group),
        provider_ready=True,
        durable_active_jobs=counts[0],
        durable_active_attempts=counts[1],
        provider_active_jobs=counts[2],
    )


async def promote_reviewed_worker(
    *,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    salad_client: I2VSaladClient,
    resolver: RuntimeSecretResolver,
    manifest_client: VersionedManifestClient,
    artifact_client_factory: ArtifactClientFactory,
    coordinates: ReviewedManifestCoordinates,
    worker_image: str,
    worker_source_revision: str,
    prepared_host_env_output: Path,
    rollback_state_output: Path,
    provider_mutation_marker_output: Path,
    timeout_seconds: float = 6_900,
) -> I2VLoraRolloutResult:
    manifest = await fetch_reviewed_manifest(manifest_client, coordinates)
    target = reviewed_target_settings(
        settings,
        worker_image=worker_image,
        worker_source_revision=worker_source_revision,
        manifest=manifest,
    )
    target_environment = await resolve_reviewed_worker_environment(target, resolver)
    artifact_client = artifact_client_factory(target_environment)
    try:
        await verify_reviewed_artifact_access(artifact_client, manifest)
    finally:
        artifact_client.close()
    write_private_host_patch(prepared_host_env_output, reviewed_host_patch(target))
    async with sessions() as session, session.begin():
        counts = await assert_zero_active_rollout_work(
            session,
            salad_client,
            queue_name=target.i2v_salad_queue_name,
        )
    group_name = target.i2v_salad_container_group_name
    prior = await salad_client.get_container_group(group_name)
    if prior.pending_change:
        raise I2VLoraRolloutError("provider group already has a pending change")
    prior_config = i2v_runtime_config_from_settings(
        settings.model_copy(update={"i2v_enabled": True})
    ).salad
    _validate_prior_group_contract(prior, prior_config)
    gpu_classes = await salad_client.list_gpu_classes()
    if not any(
        item.id == prior_config.gpu_class_id and item.name == I2V_SALAD_GPU_CLASS_NAME
        for item in gpu_classes
    ):
        raise I2VLoraRolloutError("provider GPU class is not the exact reviewed RTX 5090")
    prior_instances = await salad_client.list_container_group_instances(group_name)
    if not _has_exact_ready_instance(prior, prior_instances):
        raise I2VLoraRolloutError("provider group is not stable and ready before rollout")
    target_probe = i2v_runtime_config_from_settings(target).salad.readiness_probe_path
    state = _rollback_state(
        prior,
        promoted_image=worker_image,
        promoted_environment=target_environment,
        promoted_readiness_probe=_readiness_probe(target_probe),
    )
    write_private_json(rollback_state_output, asdict(state))
    patch = _promotion_patch(
        prior,
        worker_image=worker_image,
        environment=target_environment,
        readiness_probe_path=target_probe,
    )
    # Repeat every durable/provider guard while holding the short queue lock,
    # immediately before the sole provider mutation. A precondition failure in
    # this block must remain strictly read-only and must not rotate credentials.
    async with sessions() as session, session.begin():
        counts = await assert_zero_active_rollout_work(
            session,
            salad_client,
            queue_name=target.i2v_salad_queue_name,
        )
        current = await salad_client.get_container_group(group_name)
        _assert_group_unchanged_before_patch(current, prior)
        current_instances = await salad_client.list_container_group_instances(group_name)
        if not _has_exact_ready_instance(current, current_instances):
            raise I2VLoraRolloutError("provider group stopped being ready before rollout")
        queue = await salad_client.get_queue(target.i2v_salad_queue_name)
        if queue.current_queue_length:
            raise I2VLoraRolloutError("provider queue changed immediately before rollout")
    try:
        # Set the rollback boundary before awaiting the request because a
        # transport error can be ambiguous after Salad accepted the PATCH.
        write_private_json(
            provider_mutation_marker_output,
            {"schema": "gen-automation/i2v-lora-provider-mutation/v1"},
        )
        updated = await salad_client.update_container_group(group_name, patch)
        if str(updated.id) != state.group_id or updated.version != state.promoted_version:
            raise I2VLoraRolloutError("provider did not create the expected worker version")
        ready_group = await _wait_for_ready_group(
            salad_client,
            group_name=group_name,
            group_id=state.group_id,
            worker_image=worker_image,
            environment=target_environment,
            readiness_probe=_readiness_probe(target_probe),
            prior_contract=state.prior_contract,
            minimum_version=updated.version,
            timeout_seconds=timeout_seconds,
        )
    except BaseException:
        try:
            # A target bootstrap may legitimately take almost two hours. Mint a
            # new prior-profile lease at rollback time instead of reusing the
            # credentials resolved before the provider image pull began.
            resolved_legacy_environment = dict(
                await I2VRuntimeEnvironment(settings=settings, resolver=resolver).resolve()
            )
            legacy_environment = _reconstructed_prior_environment(
                state,
                resolved_legacy_environment,
            )
            await _restore_provider(
                salad_client,
                state=state,
                environment=legacy_environment,
                timeout_seconds=min(timeout_seconds, 1_800),
            )
            _durable_unlink(provider_mutation_marker_output)
        except BaseException:
            raise I2VLoraRolloutError(
                "provider promotion failed and automatic rollback needs operator attention"
            ) from None
        raise
    return I2VLoraRolloutResult(
        operation="promote",
        provider_image=_group_image(ready_group),
        provider_ready=True,
        durable_active_jobs=counts[0],
        durable_active_attempts=counts[1],
        provider_active_jobs=counts[2],
    )


async def rollback_reviewed_worker(
    *,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    salad_client: I2VSaladClient,
    resolver: RuntimeSecretResolver,
    state: ProviderRollbackState,
    provider_mutation_marker_output: Path | None = None,
    timeout_seconds: float = 1_800,
) -> I2VLoraRolloutResult:
    resolved_environment = dict(
        await I2VRuntimeEnvironment(settings=settings, resolver=resolver).resolve()
    )
    environment = _reconstructed_prior_environment(state, resolved_environment)
    async with sessions() as session, session.begin():
        counts = await assert_zero_active_rollout_work(
            session,
            salad_client,
            queue_name=settings.i2v_salad_queue_name,
        )
    group = await _restore_provider(
        salad_client,
        state=state,
        environment=environment,
        provider_mutation_marker_output=provider_mutation_marker_output,
        timeout_seconds=timeout_seconds,
    )
    return I2VLoraRolloutResult(
        operation="rollback",
        provider_image=_group_image(group),
        provider_ready=True,
        durable_active_jobs=counts[0],
        durable_active_attempts=counts[1],
        provider_active_jobs=counts[2],
    )


async def profile_preflight(
    *,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    salad_client: I2VSaladClient,
    resolver: RuntimeSecretResolver,
    expected_worker_image: str,
    expected_worker_source_revision: str,
    expected_public_profile: bool,
) -> I2VLoraRolloutResult:
    manifest_digest = settings.i2v_model_manifest_sha256
    if (
        not settings.i2v_enabled
        or not settings.i2v_hires_profile_enabled
        or not settings.i2v_lora_worker_enabled
        or settings.i2v_lora_profile_enabled is not expected_public_profile
        or settings.i2v_worker_image != expected_worker_image
        or settings.i2v_worker_source_revision != expected_worker_source_revision
        or settings.i2v_private_manifest_source_sha256 != REVIEWED_SOURCE_SHA256
        or manifest_digest is None
        or manifest_digest.get_secret_value() != REVIEWED_CANONICAL_MANIFEST_SHA256
        or i2v_worker_identity(settings)
        != (REVIEWED_MODEL_OBJECTS_SHA256, REVIEWED_ARTIFACT_IDENTITY_SHA256)
    ):
        raise I2VLoraRolloutError("host I2V profile does not match the expected rollout")
    group_name = settings.i2v_salad_container_group_name
    group = await salad_client.get_container_group(group_name)
    capable_config = i2v_runtime_config_from_settings(settings).salad
    _validate_prior_group_contract(group, capable_config)
    expected_environment = dict(
        await I2VRuntimeEnvironment(settings=settings, resolver=resolver).resolve()
    )
    _validate_worker_environment_identity(expected_environment, settings=settings)
    expected_probe = capable_config.readiness_probe_path
    _validate_exact_capable_group(
        group,
        worker_image=expected_worker_image,
        worker_source_revision=expected_worker_source_revision,
        readiness_probe_path=expected_probe,
        expected_environment=expected_environment,
    )
    instances = await salad_client.list_container_group_instances(group_name)
    if not _has_exact_ready_instance(group, instances):
        raise I2VLoraRolloutError("provider has no exact ready I2V worker instance")
    async with sessions() as session, session.begin():
        await _lock_i2v_queue(session)
        active_rows = (
            await session.execute(
                select(I2VAttempt, I2VJob)
                .join(I2VJob, I2VJob.id == I2VAttempt.job_id)
                .where(I2VAttempt.state.in_(_ACTIVE_ATTEMPT_STATES))
            )
        ).all()
        by_provider_id: dict[str, tuple[I2VAttempt, I2VJob]] = {}
        for attempt, job in active_rows:
            if classify_i2v_lora_settings(job.settings_snapshot) == I2VLoraSettingsKind.INVALID:
                raise I2VLoraRolloutError("an active I2V job has invalid frozen LoRA settings")
            if attempt.worker_image_digest != expected_worker_image:
                raise I2VLoraRolloutError("an active I2V attempt targets a different worker image")
            if attempt.provider_job_id is not None:
                by_provider_id[attempt.provider_job_id] = (attempt, job)
        provider_jobs = await _provider_active_jobs(
            salad_client,
            queue_name=settings.i2v_salad_queue_name,
        )
        if any(str(job.id) not in by_provider_id for job in provider_jobs):
            raise I2VLoraRolloutError("provider queue has an unmatched active I2V job")
    return I2VLoraRolloutResult(
        operation="profile-preflight",
        provider_image=_group_image(group),
        provider_ready=True,
        durable_active_jobs=len(active_rows),
        durable_active_attempts=len(active_rows),
        provider_active_jobs=len(provider_jobs),
    )


async def _provider_active_jobs(
    client: I2VSaladClient,
    *,
    queue_name: str,
) -> tuple[Any, ...]:
    active: list[Any] = []
    previous_ids: tuple[object, ...] | None = None
    page_number = 1
    while True:
        if page_number > _MAX_PROVIDER_JOB_PAGES:
            raise I2VLoraRolloutError("provider queue pagination exceeded the safety bound")
        page = await client.list_jobs(
            queue_name,
            page=page_number,
            page_size=SALAD_QUEUE_JOB_PAGE_SIZE,
        )
        page_ids = tuple(job.id for job in page.items)
        if page_ids and page_ids == previous_ids:
            raise I2VLoraRolloutError("provider queue pagination repeated a page")
        previous_ids = page_ids
        active.extend(job for job in page.items if job.status in _ACTIVE_PROVIDER_STATES)
        if len(page.items) < SALAD_QUEUE_JOB_PAGE_SIZE:
            return tuple(active)
        page_number += 1


async def _lock_i2v_queue(session: AsyncSession) -> None:
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _QUEUE_LOCK_KEY})


def _rollback_state(
    group: SaladContainerGroup,
    *,
    promoted_image: str,
    promoted_environment: Mapping[str, str],
    promoted_readiness_probe: JSONObject,
) -> ProviderRollbackState:
    container = _group_container(group)
    environment = _environment_variables(container)
    readiness = group.raw.get("readiness_probe")
    if readiness is not None and not isinstance(readiness, dict):
        raise I2VLoraRolloutError("provider readiness probe is invalid")
    return ProviderRollbackState(
        schema="gen-automation/i2v-lora-rollback/v2",
        group_id=str(group.id),
        group_name=group.name,
        prior_image=_group_image(group),
        prior_readiness_probe=deepcopy(readiness),
        prior_environment_keys=tuple(sorted(environment)),
        prior_environment_identity_sha256=_environment_identity_sha256(environment),
        prior_contract=_redacted_provider_contract(group),
        prior_version=group.version,
        promoted_image=promoted_image,
        promoted_readiness_probe=deepcopy(promoted_readiness_probe),
        promoted_environment_keys=tuple(sorted(promoted_environment)),
        promoted_environment_identity_sha256=_environment_identity_sha256(promoted_environment),
        promoted_version=group.version + 1,
    )


def _promotion_patch(
    group: SaladContainerGroup,
    *,
    worker_image: str,
    environment: Mapping[str, str],
    readiness_probe_path: str,
) -> JSONObject:
    old_environment = _environment_variables(_group_container(group))
    patched_environment: JSONObject = {
        key: None for key in old_environment if key not in environment
    }
    patched_environment.update(environment)
    return {
        "container": {
            "image": worker_image,
            "environment_variables": patched_environment,
        },
        "readiness_probe": _readiness_probe(readiness_probe_path),
    }


def _validate_prior_group_contract(
    group: SaladContainerGroup,
    config: I2VSaladConfig,
) -> None:
    try:
        validate_i2v_group_contract(group, config)
    except I2VSaladError:
        raise I2VLoraRolloutError("provider group base contract is not rollout-safe") from None
    desired = config.container_configuration()
    desired_container = desired.get("container")
    observed_container = group.raw.get("container")
    if not isinstance(desired_container, dict) or not isinstance(observed_container, dict):
        raise I2VLoraRolloutError("provider group container contract is invalid")
    exact_container_fields = ("resources", "image_caching")
    if any(
        observed_container.get(key) != desired_container.get(key) for key in exact_container_fields
    ):
        raise I2VLoraRolloutError("provider group compute contract drifted")
    for key in (
        "autostart_policy",
        "restart_policy",
        "startup_probe",
        "readiness_probe",
        "liveness_probe",
        "queue_connection",
        "queue_autoscaler",
    ):
        if group.raw.get(key) != desired.get(key):
            raise I2VLoraRolloutError("provider group scheduling contract drifted")
    if (
        str(group.id) != REVIEWED_PROVIDER_GROUP_ID
        or group.replicas != config.max_replicas
        or config.max_replicas != 1
        or group.status.lower() != "running"
        or group.current_state.running_count != 1
        or group.current_state.allocating_count
        or group.current_state.creating_count
        or group.current_state.stopping_count
    ):
        raise I2VLoraRolloutError("provider group is not the exact warm replica contract")
    expected_runtime_bindings = desired.get("runtime_bindings")
    observed_runtime_bindings = group.raw.get("runtime_bindings")
    if observed_runtime_bindings not in (expected_runtime_bindings, None):
        raise I2VLoraRolloutError("provider runtime bindings drifted")


async def _restore_provider(
    client: I2VSaladClient,
    *,
    state: ProviderRollbackState,
    environment: Mapping[str, str],
    provider_mutation_marker_output: Path | None = None,
    timeout_seconds: float,
) -> SaladContainerGroup:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    current = await client.get_container_group(state.group_name)
    if str(current.id) != state.group_id:
        raise I2VLoraRolloutError("provider group identity changed; rollback refused")
    current = await _wait_for_pending_change_clear(
        client,
        group_name=state.group_name,
        group_id=state.group_id,
        minimum_version=state.prior_version,
        timeout_seconds=min(_remaining_timeout(deadline), 600),
    )
    if tuple(sorted(environment)) != state.prior_environment_keys:
        raise I2VLoraRolloutError("rollback worker environment does not match the saved contract")
    prior_contract_matches = False
    try:
        _assert_saved_group_readback(
            current,
            state=state,
            worker_image=state.prior_image,
            environment_keys=state.prior_environment_keys,
            environment_identity_sha256=state.prior_environment_identity_sha256,
            readiness_probe=state.prior_readiness_probe,
        )
        prior_contract_matches = True
    except I2VLoraRolloutError:
        pass
    if prior_contract_matches:
        if _is_exact_stopped_group(current):
            _write_provider_mutation_marker(provider_mutation_marker_output)
            updated = await client.update_container_group(
                state.group_name,
                {"container": {"environment_variables": dict(environment)}},
            )
            if str(updated.id) != state.group_id or updated.version != current.version + 1:
                raise I2VLoraRolloutError(
                    "provider did not create the expected credential-refresh version"
                )
            await _start_exact_group_if_stopped(
                client,
                state=state,
                expected_version=updated.version,
                deadline=deadline,
            )
            return await _wait_for_ready_group(
                client,
                group_name=state.group_name,
                group_id=state.group_id,
                worker_image=state.prior_image,
                environment=environment,
                readiness_probe=state.prior_readiness_probe,
                prior_contract=state.prior_contract,
                minimum_version=updated.version,
                timeout_seconds=_remaining_timeout(deadline),
            )
        return await _wait_for_saved_prior_ready(
            client,
            state=state,
            timeout_seconds=_remaining_timeout(deadline),
        )
    try:
        _assert_saved_group_readback(
            current,
            state=state,
            worker_image=state.promoted_image,
            environment_keys=state.promoted_environment_keys,
            environment_identity_sha256=state.promoted_environment_identity_sha256,
            readiness_probe=state.promoted_readiness_probe,
        )
    except I2VLoraRolloutError as promoted_error:
        raise I2VLoraRolloutError(
            "provider contract changed outside the rollout; rollback refused"
        ) from promoted_error
    current_environment = _environment_variables(_group_container(current))
    patch_environment: JSONObject = {
        key: None for key in current_environment if key not in environment
    }
    patch_environment.update(environment)
    patch: JSONObject = {
        "container": {
            "image": state.prior_image,
            "environment_variables": patch_environment,
        },
        "readiness_probe": deepcopy(state.prior_readiness_probe),
    }
    _write_provider_mutation_marker(provider_mutation_marker_output)
    updated = await client.update_container_group(state.group_name, patch)
    if str(updated.id) != state.group_id or updated.version != current.version + 1:
        raise I2VLoraRolloutError("provider did not create the expected rollback version")
    await _start_exact_group_if_stopped(
        client,
        state=state,
        expected_version=updated.version,
        deadline=deadline,
    )
    return await _wait_for_ready_group(
        client,
        group_name=state.group_name,
        group_id=state.group_id,
        worker_image=state.prior_image,
        environment=environment,
        readiness_probe=state.prior_readiness_probe,
        prior_contract=state.prior_contract,
        minimum_version=updated.version,
        timeout_seconds=_remaining_timeout(deadline),
    )


async def _wait_for_saved_prior_ready(
    client: I2VSaladClient,
    *,
    state: ProviderRollbackState,
    timeout_seconds: float,
) -> SaladContainerGroup:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        group = await client.get_container_group(state.group_name)
        _assert_saved_group_readback(
            group,
            state=state,
            worker_image=state.prior_image,
            environment_keys=state.prior_environment_keys,
            environment_identity_sha256=state.prior_environment_identity_sha256,
            readiness_probe=state.prior_readiness_probe,
        )
        if not group.pending_change:
            instances = await client.list_container_group_instances(state.group_name)
            if _has_exact_ready_instance(group, instances):
                return group
        await asyncio.sleep(10)
    raise I2VLoraRolloutError("restored prior provider did not become ready within the bound")


async def _start_exact_group_if_stopped(
    client: I2VSaladClient,
    *,
    state: ProviderRollbackState,
    expected_version: int,
    deadline: float,
) -> None:
    settled = await _wait_for_pending_change_clear(
        client,
        group_name=state.group_name,
        group_id=state.group_id,
        minimum_version=expected_version,
        timeout_seconds=_remaining_timeout(deadline),
    )
    if settled.version != expected_version:
        raise I2VLoraRolloutError("provider version changed before rollback restart")
    if _is_exact_stopped_group(settled):
        await client.start_container_group(state.group_name)


async def _wait_for_ready_group(
    client: I2VSaladClient,
    *,
    group_name: str,
    group_id: str,
    worker_image: str,
    environment: Mapping[str, str],
    readiness_probe: JSONObject | None,
    prior_contract: JSONObject,
    minimum_version: int,
    timeout_seconds: float,
) -> SaladContainerGroup:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        group = await client.get_container_group(group_name)
        if str(group.id) != group_id:
            raise I2VLoraRolloutError("provider group identity changed during rollout")
        if group.version > minimum_version:
            raise I2VLoraRolloutError("provider group version changed outside the rollout")
        if group.version == minimum_version and not group.pending_change:
            _assert_exact_group_readback(
                group,
                group_id=group_id,
                worker_image=worker_image,
                environment=environment,
                readiness_probe=readiness_probe,
                prior_contract=prior_contract,
            )
            instances = await client.list_container_group_instances(group_name)
            if _has_exact_ready_instance(group, instances):
                return group
        await asyncio.sleep(10)
    raise I2VLoraRolloutError("provider worker did not reach exact readiness within the bound")


async def _wait_for_pending_change_clear(
    client: I2VSaladClient,
    *,
    group_name: str,
    group_id: str,
    minimum_version: int,
    timeout_seconds: float,
) -> SaladContainerGroup:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        group = await client.get_container_group(group_name)
        if str(group.id) != group_id:
            raise I2VLoraRolloutError("provider group identity changed during rollback")
        if group.version < minimum_version:
            raise I2VLoraRolloutError(
                "provider version moved behind the saved rollout; rollback refused"
            )
        if not group.pending_change:
            return group
        await asyncio.sleep(10)
    raise I2VLoraRolloutError("provider group pending change did not settle before rollback")


def _assert_saved_group_readback(
    group: SaladContainerGroup,
    *,
    state: ProviderRollbackState,
    worker_image: str,
    environment_keys: tuple[str, ...],
    environment_identity_sha256: str,
    readiness_probe: JSONObject | None,
) -> None:
    if str(group.id) != state.group_id or _group_image(group) != worker_image:
        raise I2VLoraRolloutError("provider saved image or identity changed")
    environment = _environment_variables(_group_container(group))
    if (
        tuple(sorted(environment)) != environment_keys
        or _environment_identity_sha256(environment) != environment_identity_sha256
    ):
        raise I2VLoraRolloutError("provider saved worker environment identity changed")
    if group.raw.get("readiness_probe") != readiness_probe:
        raise I2VLoraRolloutError("provider saved readiness probe changed")
    expected_contract = deepcopy(state.prior_contract)
    expected_container = cast(JSONObject, expected_contract["container"])
    expected_container["image"] = worker_image
    expected_container["environment_variable_keys"] = cast(list[JSONValue], list(environment_keys))
    if readiness_probe is None:
        expected_contract.pop("readiness_probe", None)
    else:
        expected_contract["readiness_probe"] = readiness_probe
    observed_contract = _redacted_provider_contract(group)
    if _is_exact_stopped_group(group):
        observed_contract["replicas"] = expected_contract.get("replicas", 1)
    if observed_contract != expected_contract:
        raise I2VLoraRolloutError("provider saved container contract drifted")


def _is_exact_stopped_group(group: SaladContainerGroup) -> bool:
    """Recognize Salad's normal warm-idle state without relaxing config identity."""

    return (
        not group.pending_change
        and group.replicas == 0
        and group.status.lower() == "stopped"
        and group.current_state.running_count == 0
        and group.current_state.allocating_count == 0
        and group.current_state.creating_count == 0
        and group.current_state.stopping_count == 0
    )


def _assert_exact_group_readback(
    group: SaladContainerGroup,
    *,
    group_id: str,
    worker_image: str,
    environment: Mapping[str, str],
    readiness_probe: JSONObject | None,
    prior_contract: JSONObject,
    **_kwargs: object,
) -> None:
    if str(group.id) != group_id or _group_image(group) != worker_image:
        raise I2VLoraRolloutError("provider group image or identity readback changed")
    observed_environment = _environment_variables(_group_container(group))
    if observed_environment != dict(environment):
        raise I2VLoraRolloutError("provider worker environment readback changed")
    if group.raw.get("readiness_probe") != readiness_probe:
        raise I2VLoraRolloutError("provider readiness probe readback changed")
    observed_contract = _redacted_provider_contract(group)
    expected_contract = deepcopy(prior_contract)
    expected_container = cast(JSONObject, expected_contract["container"])
    expected_container["image"] = worker_image
    expected_container["environment_variable_keys"] = cast(list[JSONValue], sorted(environment))
    if readiness_probe is None:
        expected_contract.pop("readiness_probe", None)
    else:
        expected_contract["readiness_probe"] = readiness_probe
    if observed_contract != expected_contract:
        raise I2VLoraRolloutError("provider container contract drifted during rollout")


def _validate_exact_capable_group(
    group: SaladContainerGroup,
    *,
    worker_image: str,
    worker_source_revision: str,
    readiness_probe_path: str,
    expected_environment: Mapping[str, str],
) -> None:
    if group.pending_change:
        raise I2VLoraRolloutError("provider worker still has a pending change")
    if _group_image(group) != worker_image:
        raise I2VLoraRolloutError("provider worker image differs from the expected digest")
    if group.raw.get("readiness_probe") != _readiness_probe(readiness_probe_path):
        raise I2VLoraRolloutError("provider readiness probe differs from the exact capability")
    environment = _environment_variables(_group_container(group))
    _validate_worker_environment_identity(environment, settings=None)
    _validate_exact_environment_readback(environment, expected_environment)
    if environment.get("GEN_I2V_WORKER_SOURCE_REVISION") != worker_source_revision:
        raise I2VLoraRolloutError("provider worker source revision changed")


def _validate_worker_environment_identity(
    environment: Mapping[str, str],
    *,
    settings: Settings | None,
) -> None:
    model_objects = environment.get("GEN_I2V_WORKER_MODEL_OBJECTS_JSON", "")
    if hashlib.sha256(model_objects.encode("utf-8")).hexdigest() != REVIEWED_MODEL_OBJECTS_SHA256:
        raise I2VLoraRolloutError("derived worker model-object digest changed")
    try:
        objects = json.loads(model_objects)
        identity = [
            {
                "role": item["role"],
                "byte_size": item["byte_size"],
                "sha256": item["sha256"],
                "version_id": item["version_id"],
            }
            for item in objects
        ]
    except (KeyError, TypeError, ValueError):
        raise I2VLoraRolloutError("derived worker artifact identity is invalid") from None
    encoded = json.dumps(identity, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != REVIEWED_ARTIFACT_IDENTITY_SHA256:
        raise I2VLoraRolloutError("reviewed artifact identity changed")
    expected_revision = settings.i2v_worker_source_revision if settings is not None else None
    if environment.get("GEN_I2V_WORKER_LORA_WORKER_ENABLED") != "true":
        raise I2VLoraRolloutError("provider worker capability is not enabled")
    if environment.get("GEN_I2V_WORKER_PRIVATE_MANIFEST_SOURCE_SHA256") != REVIEWED_SOURCE_SHA256:
        raise I2VLoraRolloutError("provider private source manifest identity changed")
    if (
        expected_revision is not None
        and environment.get("GEN_I2V_WORKER_SOURCE_REVISION") != expected_revision
    ):
        raise I2VLoraRolloutError("provider worker source revision changed")
    if any(not environment.get(key) for key in _WORKER_CREDENTIAL_KEYS):
        raise I2VLoraRolloutError("provider worker artifact credentials are incomplete")


def _validate_exact_environment_readback(
    observed: Mapping[str, str],
    expected: Mapping[str, str],
) -> None:
    if set(observed) != set(expected):
        raise I2VLoraRolloutError("provider worker environment keys changed")
    for key, value in expected.items():
        if key not in _WORKER_CREDENTIAL_KEYS and observed.get(key) != value:
            raise I2VLoraRolloutError("provider worker environment identity changed")
    if any(not observed.get(key) for key in _WORKER_CREDENTIAL_KEYS):
        raise I2VLoraRolloutError("provider worker artifact credentials are incomplete")


def _redacted_provider_contract(group: SaladContainerGroup) -> JSONObject:
    keys = (
        "display_name",
        "replicas",
        "autostart_policy",
        "restart_policy",
        "container",
        "startup_probe",
        "readiness_probe",
        "liveness_probe",
        "queue_connection",
        "queue_autoscaler",
        "runtime_bindings",
        "priority",
    )
    contract: JSONObject = {
        key: deepcopy(group.raw[key]) for key in keys if key in group.raw and key != "container"
    }
    container = deepcopy(_group_container(group))
    environment = _environment_variables(container)
    container.pop("environment_variables", None)
    container.pop("registry_authentication", None)
    container["environment_variable_keys"] = cast(list[JSONValue], sorted(environment))
    contract["container"] = container
    return contract


def _group_container(group: SaladContainerGroup) -> JSONObject:
    value = group.raw.get("container")
    if not isinstance(value, dict):
        raise I2VLoraRolloutError("provider group container contract is invalid")
    return value


def _environment_variables(container: Mapping[str, JSONValue]) -> dict[str, str]:
    value = container.get("environment_variables", {})
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) and item for key, item in value.items()
    ):
        raise I2VLoraRolloutError("provider worker environment is invalid")
    return cast(dict[str, str], dict(value))


def _environment_identity_sha256(environment: Mapping[str, str]) -> str:
    identity = {
        key: value for key, value in environment.items() if key not in _WORKER_CREDENTIAL_KEYS
    }
    encoded = json.dumps(identity, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _reconstructed_prior_environment(
    state: ProviderRollbackState,
    resolved: Mapping[str, str],
) -> dict[str, str]:
    if any(key not in resolved for key in state.prior_environment_keys):
        raise I2VLoraRolloutError("prior worker environment cannot be reconstructed")
    result = {key: resolved[key] for key in state.prior_environment_keys}
    if _environment_identity_sha256(result) != state.prior_environment_identity_sha256:
        raise I2VLoraRolloutError("prior worker environment identity changed")
    return result


def _group_image(group: SaladContainerGroup) -> str:
    image = _group_container(group).get("image")
    if not isinstance(image, str) or not image:
        raise I2VLoraRolloutError("provider group image is invalid")
    return image


def _readiness_probe(path: str) -> JSONObject:
    return {
        "http": {"headers": [], "path": path, "port": 8000, "scheme": "http"},
        "initial_delay_seconds": 0,
        "period_seconds": 5,
        "timeout_seconds": 3,
        "success_threshold": 1,
        "failure_threshold": 3,
    }


def _has_exact_ready_instance(group: SaladContainerGroup, page: object) -> bool:
    instances = getattr(page, "instances", None)
    if not isinstance(instances, tuple) or any(
        not isinstance(item, SaladContainerGroupInstance) for item in instances
    ):
        raise I2VLoraRolloutError("provider instance readback is invalid")
    if (
        group.pending_change
        or group.replicas != 1
        or group.status.lower() != "running"
        or group.current_state.running_count != 1
        or group.current_state.allocating_count
        or group.current_state.creating_count
        or group.current_state.stopping_count
        or len(instances) != 1
    ):
        return False
    return all(
        instance.version == group.version
        and instance.state.value == "running"
        and instance.ready is True
        and instance.started is True
        for instance in instances
    )


def _assert_group_unchanged_before_patch(
    current: SaladContainerGroup,
    prior: SaladContainerGroup,
) -> None:
    if (
        str(current.id) != str(prior.id)
        or current.name != prior.name
        or current.version != prior.version
        or current.pending_change
        or _redacted_provider_contract(current) != _redacted_provider_contract(prior)
        or _environment_identity_sha256(_environment_variables(_group_container(current)))
        != _environment_identity_sha256(_environment_variables(_group_container(prior)))
    ):
        raise I2VLoraRolloutError("provider group changed before rollout")


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise I2VLoraRolloutError("provider rollback exceeded its bounded deadline")
    return remaining


def _write_provider_mutation_marker(path: Path | None) -> None:
    if path is not None:
        write_private_json(
            path,
            {"schema": "gen-automation/i2v-lora-provider-mutation/v1"},
        )


def _durable_unlink(path: Path) -> None:
    path.unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _atomic_private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
