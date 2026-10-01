"""Long, credential-bearing grants must survive controller and worker parsing."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from botocore.credentials import RefreshableCredentials
from pydantic import ValidationError

from gen_automation.i2v_worker.media import MediaError, validate_grant_url
from gen_automation.i2v_worker.models import (
    MAX_SIGNED_GRANT_URL_LENGTH,
    DownloadGrant,
    H3LoraGrant,
    I2VJob,
    UploadGrant,
)
from gen_automation.services.i2v_media import I2VSignedGrantBuilder
from gen_automation.services.i2v_runtime import I2VRuntimeConfigurationError, _validate_fresh_grants
from gen_automation.storage.s3 import S3ObjectStore
from tests.test_h3_base_video_diagnostic import _snapshots
from tests.test_i2v_worker_contract import _job


def _long_url(length):
    prefix = "https://private.example.test/video?X-Amz-Security-Token="
    return prefix + "x" * (length - len(prefix))


@pytest.mark.parametrize("length", [2083, 2090, 2118, 8192, MAX_SIGNED_GRANT_URL_LENGTH])
def test_long_input_output_and_lora_grants_round_trip_without_signature_changes(length):
    raw = _job()
    url = _long_url(length)
    raw["input_grant"]["url"] = url
    raw["output_grant"]["url"] = url
    parsed = I2VJob.model_validate_json(json.dumps(raw), strict=True)
    assert str(parsed.input_grant.url) == url
    assert str(parsed.output_grant.url) == url
    lora = H3LoraGrant.model_validate(
        {
            "artifact_id": raw["job_id"],
            "sha256": "a" * 64,
            "byte_size": 1,
            "download": raw["input_grant"],
        }
    )
    assert str(lora.download.url) == url


@pytest.mark.parametrize(
    "model,field", [(DownloadGrant, "input_grant"), (UploadGrant, "output_grant")]
)
@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/video",
        "ftp://example.test/video",
        "not-a-url",
        "https://",
        _long_url(MAX_SIGNED_GRANT_URL_LENGTH + 1),
    ],
)
def test_grant_limits_and_http_schemes_remain_enforced(model, field, url):
    raw = _job()[field]
    raw["url"] = url
    with pytest.raises(ValidationError):
        model.model_validate(raw)


@pytest.mark.parametrize(
    "header,value",
    [
        ("Content-Type", "text/html"),
        ("Cache-Control", "public"),
        ("x-amz-server-side-encryption", ""),
        ("Authorization", "not-allowed"),
    ],
)
def test_long_grants_do_not_relax_private_upload_headers(header, value):
    raw = _job()["output_grant"]
    raw["url"] = _long_url(4096)
    raw["headers"][header] = value
    with pytest.raises(ValidationError):
        UploadGrant.model_validate(raw)


async def test_real_botocore_long_session_grants_pass_controller_and_worker_offline():
    # Explicit fake credentials prevent metadata/network credential discovery.
    store = S3ObjectStore(
        bucket="private",
        region="eu-central-1",
        access_key_id="test-access-id",
        secret_access_key="test-secret",  # noqa: S106
        session_token="test-session/+=encoded" * 180,
        delivery_domain="dtest.cloudfront.net",
    )
    credentials = {
        "access_key": "test-access-id",
        "secret_key": "test-secret",
        "token": "test-session/+=encoded" * 180,
        "expiry_time": (datetime.now(UTC) + timedelta(hours=6)).isoformat(),
    }
    store.client._request_signer._credentials = RefreshableCredentials.create_from_metadata(
        metadata=credentials,
        refresh_using=lambda: credentials,
        method="test-only",
    )
    job, attempt = _snapshots()
    grants = await I2VSignedGrantBuilder(store=store, expires_in=3600).build(
        job=job, attempt=attempt
    )
    _validate_fresh_grants(
        grants, job=job, attempt=attempt, output_prefix="i2v/outputs", now=datetime.now(UTC)
    )
    raw = _job()
    raw.update(
        job_id=str(job.job_id),
        attempt_id=str(attempt.attempt_id),
        settings_snapshot=job.settings_snapshot,
        **grants,
    )
    parsed = I2VJob.model_validate_json(json.dumps(raw), strict=True)
    for name in ("input_grant", "output_grant", "base_video_grant"):
        original = grants[name]["url"]
        assert 2083 < len(original) <= MAX_SIGNED_GRANT_URL_LENGTH
        assert str(getattr(parsed, name).url) == original
        assert "%2F" in original and "%2B" in original
    # Attempt binding and freshness checks are not bypassed by the larger cap.
    grants["base_video_grant"]["expires_at"] = (
        datetime.now(UTC) - timedelta(seconds=1)
    ).isoformat()
    with pytest.raises(I2VRuntimeConfigurationError, match="diagnostic grant does not match"):
        _validate_fresh_grants(
            grants, job=job, attempt=attempt, output_prefix="i2v/outputs", now=datetime.now(UTC)
        )


@pytest.mark.parametrize(
    "prefix", ["http://example.test/", "https://user:pass@example.test/", "https://example.test/#"]
)
def test_production_transport_still_rejects_insecure_or_ambiguous_long_grants(prefix):
    with pytest.raises(MediaError):
        validate_grant_url(prefix + "x" * 3000, allow_http=False)
