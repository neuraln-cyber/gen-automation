import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from gen_automation.app import _build_model_object_store
from gen_automation.config import Environment, Settings
from gen_automation.domain.private_delivery import PrivateDeliveryRoute
from gen_automation.middleware import content_security_policy
from gen_automation.storage.s3 import S3ObjectStore

DOMAIN = "d123example.cloudfront.net"


def test_infrastructure_never_caches_or_grants_public_origin_access() -> None:
    template = (
        Path(__file__).resolve().parents[1] / "infra/aws-private-delivery/template.yaml"
    ).read_text()
    assert (
        re.findall(r"CachePolicyId: (\S+)", template)
        == [
            "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
        ]
        * 2
    )
    assert (
        re.findall(r"OriginRequestPolicyId: (\S+)", template)
        == [
            "b689b0a8-53d0-40ab-baf2-68738e2966ac",
        ]
        * 2
    )
    assert "PlanTier: PRO" in template
    assert "SampledRequestsEnabled: false" in template
    assert "AWS::S3::BucketPolicy" not in template
    assert "AWS::IAM::" not in template
    assert "OriginAccessControl" not in template
    assert "Logging:" not in template


@pytest.mark.asyncio
async def test_external_model_grants_use_model_origin_not_asset_origin() -> None:
    store = _build_model_object_store(
        Settings(
            salad_worker_artifact_bucket="private-models",
            salad_worker_artifact_region="eu-central-1",
            salad_worker_artifact_access_key_id="test-key",
            salad_worker_artifact_secret_access_key="test-secret",  # noqa: S106
            salad_worker_artifact_delivery_domain=DOMAIN,
        )
    )
    try:
        url = await store.presign_download(key="models/model.bin", expires_in=120, version_id="v1")
        parsed = urlsplit(url)
        assert parsed.hostname == DOMAIN
        assert parsed.path == "/models/models/model.bin"
        assert parse_qs(parsed.query)["versionId"] == ["v1"]
    finally:
        await store.close()


def test_browser_policy_allows_delivery_without_breaking_direct_uploads() -> None:
    policy = content_security_policy(
        Environment.PRODUCTION,
        asset_connect_source="https://private-assets.s3.eu-central-1.amazonaws.com",
        asset_delivery_connect_source=f"https://{DOMAIN}",
    )
    directives = {part.strip().split(" ", 1)[0]: part for part in policy.split(";") if part.strip()}
    for name in ("connect-src", "media-src"):
        assert DOMAIN in directives[name]
        assert "private-assets.s3.eu-central-1.amazonaws.com" in directives[name]
        assert "*" not in directives[name]


def test_route_preserves_encoded_path_and_query_exactly() -> None:
    route = PrivateDeliveryRoute(DOMAIN, "private-assets", "eu-central-1", "assets")
    path = "/nested/space%20and%2Bplus/%E6%A8%A1.bin"
    query = "versionId=a%2Fb%2Bc%3D&X-Amz-Signature=abc&response-content-disposition=x%20y"
    assert route.rewrite(f"https://{route.origin}{path}?{query}") == (
        f"https://{DOMAIN}/assets{path}?{query}"
    )


@pytest.mark.parametrize(
    "domain",
    [
        "example.org",
        "https://d123.cloudfront.net",
        "d123.cloudfront.net.evil.test",
        "d123.cloudfront.net/path",
        "user@d123.cloudfront.net",
        "d123.cloudfront.net:443",
        "D123.cloudfront.net",
        "d123.cloudfront.net\n",
    ],
)
def test_rejects_untrusted_delivery_configuration(domain: str) -> None:
    with pytest.raises(ValueError, match="CloudFront"):
        PrivateDeliveryRoute(domain, "private-assets", "eu-central-1", "assets")


@pytest.mark.parametrize(
    "url",
    [
        "http://private-assets.s3.eu-central-1.amazonaws.com/file",
        "https://other-bucket.s3.eu-central-1.amazonaws.com/file",
        "https://private-assets.s3.us-east-1.amazonaws.com/file",
        "https://private-assets.s3.eu-central-1.amazonaws.com.evil.test/file",
        "https://user@private-assets.s3.eu-central-1.amazonaws.com/file",
        "https://private-assets.s3.eu-central-1.amazonaws.com:443/file",
        "https://private-assets.s3.eu-central-1.amazonaws.com/file#fragment",
    ],
)
def test_rejects_unexpected_signing_origin_without_leaking_url(url: str) -> None:
    route = PrivateDeliveryRoute(DOMAIN, "private-assets", "eu-central-1", "assets")
    with pytest.raises(ValueError, match="unexpected S3 origin") as caught:
        route.rewrite(url + "?secret-value")
    assert "secret-value" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("region", ["eu-central-1", "us-east-1"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("named", [False, True])
async def test_only_unnamed_download_grants_use_delivery(
    region: str,
    enabled: bool,
    named: bool,
) -> None:
    store = S3ObjectStore(
        bucket="private-assets",
        region=region,
        access_key_id="test-key",
        secret_access_key="test-secret",  # noqa: S106
        delivery_domain=DOMAIN if enabled else None,
    )
    try:
        url = await store.presign_download(
            key="folder/test file+.bin",
            version_id="exact/version+id",
            download_name="my file.bin" if named else None,
            expires_in=120,
        )
        parsed = urlsplit(url)
        assert parsed.hostname == (
            DOMAIN
            if enabled and not named
            else (
                "private-assets.s3.amazonaws.com"
                if region == "us-east-1" and not enabled
                else f"private-assets.s3.{region}.amazonaws.com"
            )
        )
        assert (
            parsed.path
            == ("/assets" if enabled and not named else "") + "/folder/test%20file%2B.bin"
        )
        query = parse_qs(parsed.query)
        assert query["versionId"] == ["exact/version+id"]
        assert query["X-Amz-Expires"] == ["120"]
        assert "X-Amz-Signature" in query
        assert query["response-cache-control"] == ["private, no-store, max-age=0"]
        if named:
            assert query["response-content-disposition"] == [
                "attachment; filename*=UTF-8''my%20file.bin"
            ]
        else:
            assert "response-content-disposition" not in query
        upload = await store.presign_put(
            key="upload.bin",
            content_type="application/octet-stream",
            metadata={},
            expires_in=120,
        )
        assert urlsplit(upload.url).hostname != DOMAIN
    finally:
        await store.close()


@pytest.mark.parametrize(
    "field,bucket,region",
    [
        ("storage_delivery_domain", "storage_bucket", "storage_region"),
        (
            "salad_worker_artifact_delivery_domain",
            "salad_worker_artifact_bucket",
            "salad_worker_artifact_region",
        ),
    ],
)
def test_delivery_flags_are_independent_validated_and_off_by_default(
    field: str,
    bucket: str,
    region: str,
) -> None:
    assert getattr(Settings(), field) is None
    with pytest.raises(ValidationError):
        Settings(**{field: DOMAIN})
    settings = Settings(**{field: DOMAIN, bucket: "private-assets", region: "eu-central-1"})
    assert getattr(settings, field) is not None
    endpoint = (
        "storage_endpoint_url"
        if field.startswith("storage")
        else "salad_worker_artifact_endpoint_url"
    )
    with pytest.raises(ValidationError, match="custom storage endpoint"):
        Settings(
            **{
                field: DOMAIN,
                bucket: "private-assets",
                region: "eu-central-1",
                endpoint: "https://custom.example.test",
            }
        )
