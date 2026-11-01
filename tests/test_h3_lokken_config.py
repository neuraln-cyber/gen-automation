"""Actual control-plane validation at the Beta7-to-Lokken settings boundary."""

import hashlib
import json

import pytest
from pydantic import ValidationError

from gen_automation.config import Settings
from gen_automation.i2v_worker.h3_upscale import (
    H3_UPSCALER_BYTES,
    H3_UPSCALER_FILENAME,
    H3_UPSCALER_ROLE,
    H3_UPSCALER_SHA256,
)
from gen_automation.i2v_worker.h3_variants import H3_EROS_BETA7_MODELS, H3_LOKKEN_MODELS
from gen_automation.i2v_worker.models import GenerationSettings
from gen_automation.services.i2v_environment import (
    i2v_salad_runtime_config_from_settings,
    i2v_worker_model_objects,
)


def manifest_values(models):
    raw = json.dumps(
        {
            "schema": "gen-automation/i2v-private-model-mirror/v1",
            "objects": [
                {
                    "role": role,
                    "sha256": sha,
                    "bytes": size,
                    "target_filename": filename,
                    "key": f"worker/i2v/sha256/{sha}",
                    "version_id": "immutable-test-version",
                }
                for role, (sha, size, filename) in models.items()
            ],
        },
        sort_keys=True,
    )
    return {
        "i2v_model_manifest_json": raw,
        "i2v_model_manifest_sha256": hashlib.sha256(raw.encode()).hexdigest(),
    }


def legacy_settings(source_resolution=True):
    return Settings(
        _env_file=None,
        environment="test",
        background_runtime_enabled=True,
        storage_enabled=True,
        storage_bucket="private-assets",
        storage_delivery_domain="d123abc.cloudfront.net",
        salad_enabled=True,
        salad_api_key="test-key",
        salad_organization="organization",
        salad_project="project",
        salad_queue_name="generation-queue",
        salad_container_group_name="generation-workers",
        salad_webhook_secret="whsec_test",  # noqa: S106
        salad_worker_image=f"registry.example/worker@sha256:{'a' * 64}",
        salad_worker_artifact_bucket="private-models",
        salad_worker_artifact_region="eu-central-1",
        salad_worker_artifact_delivery_domain="d123abc.cloudfront.net",
        i2v_enabled=True,
        i2v_profile="minimax_h3",
        i2v_h3_model_variant="eros_beta7",
        i2v_h3_source_resolution_enabled=source_resolution,
        i2v_h3_advanced_sampling_enabled=True,
        i2v_salad_gpu_class_id="00000000-0000-0000-0000-000000000001",
        i2v_worker_image=f"registry.example/i2v-worker@sha256:{'b' * 64}",
        i2v_worker_source_revision="c" * 40,
        i2v_private_manifest_source_sha256="d" * 64,
        i2v_require_private_delivery=True,
        **manifest_values(
            {
                **H3_EROS_BETA7_MODELS,
                H3_UPSCALER_ROLE: (
                    H3_UPSCALER_SHA256,
                    H3_UPSCALER_BYTES,
                    H3_UPSCALER_FILENAME,
                ),
            }
        ),
    )


def candidate_values(original, models=None):
    return {
        **original.model_dump(),
        **manifest_values(H3_LOKKEN_MODELS if models is None else models),
        "i2v_h3_model_variant": "eros_beta7_lokken",
        "i2v_worker_image": f"registry.example/i2v-worker@sha256:{'e' * 64}",
        "i2v_worker_source_revision": "f" * 40,
        "i2v_private_manifest_source_sha256": "0" * 64,
    }


@pytest.mark.parametrize("source_resolution", [False, True])
def test_lokken_cutover_validates_without_changing_existing_capability_flags(source_resolution):
    original = legacy_settings(source_resolution)
    candidate = Settings.model_validate(candidate_values(original))
    bindings = {
        "i2v_worker_image",
        "i2v_worker_source_revision",
        "i2v_model_manifest_json",
        "i2v_model_manifest_sha256",
        "i2v_private_manifest_source_sha256",
        "i2v_h3_model_variant",
    }
    assert {k: v for k, v in original.model_dump().items() if k not in bindings} == {
        k: v for k, v in candidate.model_dump().items() if k not in bindings
    }
    assert candidate.i2v_h3_source_resolution_enabled is source_resolution
    assert {obj.role for obj in i2v_worker_model_objects(candidate)} == set(H3_LOKKEN_MODELS)
    runtime = i2v_salad_runtime_config_from_settings(candidate)
    assert runtime.salad.worker_image == candidate.i2v_worker_image
    # Preserving the legacy capability flag does not change Lokken's MP workflow.
    with pytest.raises(ValidationError, match="MP-based output"):
        GenerationSettings(
            profile="minimax_h3",
            h3_model_variant="eros_beta7_lokken",
            match_source_resolution=True,
        )


@pytest.mark.parametrize("role", list(H3_LOKKEN_MODELS))
@pytest.mark.parametrize("mutation", ["missing", "hash", "size", "filename"])
def test_lokken_cutover_still_rejects_missing_or_substituted_models(role, mutation):
    models = dict(H3_LOKKEN_MODELS)
    sha, size, filename = models[role]
    if mutation == "missing":
        del models[role]
    elif mutation == "hash":
        models[role] = ("a" * 64, size, filename)
    elif mutation == "size":
        models[role] = (sha, size + 1, filename)
    else:
        models[role] = (sha, size, "substituted.safetensors")
    with pytest.raises(ValidationError):
        Settings.model_validate(candidate_values(legacy_settings(), models))


def test_legacy_source_resolution_still_requires_its_own_pinned_upscaler():
    values = legacy_settings().model_dump()
    values.update(manifest_values(H3_EROS_BETA7_MODELS))
    with pytest.raises(ValidationError, match="source-resolution delivery requires the pinned"):
        Settings.model_validate(values)


def test_lokken_upscaler_cannot_satisfy_legacy_source_size_capability():
    values = legacy_settings().model_dump()
    values.update(
        manifest_values(
            {
                **H3_EROS_BETA7_MODELS,
                "h3_lokken_upscaler": H3_LOKKEN_MODELS["h3_lokken_upscaler"],
            }
        )
    )
    with pytest.raises(ValidationError, match="source-resolution delivery requires the pinned"):
        Settings.model_validate(values)
