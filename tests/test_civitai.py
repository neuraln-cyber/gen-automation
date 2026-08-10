import json
import socket
from typing import Any, cast

import httpcore2
import httpx2
import pytest

from gen_automation.integrations.civitai import (
    CivitaiAPIError,
    CivitaiClient,
    CivitaiModelType,
    CivitaiResolvedLora,
    CivitaiSourceKind,
    CivitaiSourceSelectionError,
    CivitaiTransportError,
    CivitaiURLValidationError,
    parse_civitai_url,
)
from gen_automation.integrations.civitai.models import (
    CivitaiFileScan,
    CivitaiLicenseTerms,
)
from gen_automation.integrations.civitai.transport import (
    PublicOnlyNetworkBackend,
    is_public_ip,
    sanitize_download_destination,
    validate_public_addresses,
)

TOKEN = "civitai-test-token-that-must-stay-secret"  # noqa: S105
SHA256 = "a" * 64


def version_payload(
    *,
    version_id: int = 456,
    model_id: int = 123,
    minor: bool = False,
    scan: str = "Success",
    size_kb: float = 1.25,
) -> dict[str, Any]:
    return {
        "id": version_id,
        "modelId": model_id,
        "name": "V1",
        "baseModel": "SDXL 1.0",
        "trainedWords": ["test-style"],
        "status": "Published",
        "minor": minor,
        "files": [
            {
                "id": 789,
                "name": "Creator_LoRA.safetensors",
                "type": "Model",
                "sizeKB": size_kb,
                "primary": True,
                "metadata": {"format": "SafeTensor"},
                "hashes": {"SHA256": SHA256.upper()},
                "pickleScanResult": scan,
                "virusScanResult": "Success",
                "downloadUrl": (
                    "https://civitai.com/api/download/models/456?type=Model&format=SafeTensor"
                ),
            }
        ],
    }


def model_payload(
    *,
    model_type: str = "LORA",
    minor: bool = False,
    commercial_use: str | list[str] = "Image",
    version: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected_version = version or version_payload()
    return {
        "id": 123,
        "name": "Creator LoRA",
        "type": model_type,
        "nsfw": True,
        "nsfwLevel": 4,
        "minor": minor,
        "creator": {"username": "creator"},
        "allowNoCredit": False,
        "allowCommercialUse": commercial_use,
        "allowDerivatives": True,
        "allowDifferentLicense": False,
        "modelVersions": [selected_version],
    }


@pytest.mark.parametrize(
    ("value", "kind", "model_id", "version_id"),
    [
        (
            "https://www.civitai.com/models/123/a-model?modelVersionId=456",
            CivitaiSourceKind.MODEL,
            123,
            456,
        ),
        (
            "https://civitai.com/api/v1/model-versions/456",
            CivitaiSourceKind.VERSION,
            None,
            456,
        ),
        (
            "https://civitai.com/api/download/models/456?type=Model&format=SafeTensor",
            CivitaiSourceKind.DOWNLOAD,
            None,
            456,
        ),
        (
            "https://civitai.red/models/196908/disgusted-face-illustriousponysd15-or-goofy-ai"
            "?modelVersionId=2372164",
            CivitaiSourceKind.MODEL,
            196908,
            2372164,
        ),
        (
            "https://civitai.red/models/2836680/biting-own-lips-or-goofy-ai",
            CivitaiSourceKind.MODEL,
            2836680,
            None,
        ),
    ],
)
def test_parse_civitai_url_accepts_only_known_contracts(
    value: str,
    kind: CivitaiSourceKind,
    model_id: int | None,
    version_id: int | None,
) -> None:
    result = parse_civitai_url(value)
    assert result.kind == kind
    assert result.model_id == model_id
    assert result.version_id == version_id
    assert result.canonical_url.startswith("https://civitai.com/")


@pytest.mark.parametrize(
    "value",
    [
        "http://civitai.com/models/123",
        "https://secret@civitai.com/models/123",
        "https://civitai.com:444/models/123",
        "https://civitai.com/models/%31%32%33",
        "https://civitai.example/models/123",
        "https://civitai.com/models/123?token=secret",
        "https://civitai.com/models/123?modelVersionId=456&modelVersionId=457",
        "https://civitai.com/api/download/models/456?apiKey=secret",
        "https://civitai.com/other/123",
    ],
)
def test_parse_civitai_url_rejects_credentials_ambiguity_and_unknown_paths(value: str) -> None:
    with pytest.raises(CivitaiURLValidationError) as captured:
        parse_civitai_url(value)
    assert "secret" not in str(captured.value)


@pytest.mark.asyncio
async def test_model_only_url_requires_explicit_version_without_requesting_metadata() -> None:
    async def handler(_request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("resolver must not silently pick a model version")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
        client = CivitaiClient(http_client=http_client)
        with pytest.raises(CivitaiSourceSelectionError, match="explicit"):
            await client.resolve_lora("https://civitai.com/models/123")


@pytest.mark.asyncio
async def test_version_choices_are_bounded_and_contain_no_download_handle() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/api/v1/models/123"
        return httpx2.Response(200, json=model_payload(), request=request)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
        client = CivitaiClient(api_token=TOKEN, http_client=http_client)
        choices = await client.list_lora_versions("https://civitai.com/models/123")

    assert len(choices) == 1
    assert choices[0].version_id == 456
    assert choices[0].target_filename == "Creator-LoRA.safetensors"
    assert "download" not in repr(choices[0]).casefold()
    assert TOKEN not in repr(client)
    assert TOKEN not in repr(client.__dict__)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_type", "expected_type"),
    [
        ("LORA", CivitaiModelType.LORA),
        ("LoCon", CivitaiModelType.LOCON),
        ("DoRA", CivitaiModelType.DORA),
    ],
)
async def test_resolve_explicit_version_supports_managed_lora_types(
    raw_type: str,
    expected_type: CivitaiModelType,
) -> None:
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path == "/api/v1/model-versions/456":
            return httpx2.Response(200, json=version_payload(), request=request)
        return httpx2.Response(
            200,
            json=model_payload(model_type=raw_type),
            request=request,
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
        client = CivitaiClient(api_token=TOKEN, http_client=http_client)
        resolved = await client.resolve_lora("https://civitai.com/models/123?modelVersionId=456")

    assert resolved.model_type == expected_type
    assert resolved.sha256 == SHA256
    assert resolved.license_terms.commercial_use == ("Image",)
    assert resolved.target_filename == "Creator-LoRA.safetensors"
    assert all(request.headers["authorization"] == f"Bearer {TOKEN}" for request in requests)
    assert "downloadUrl" not in json.dumps(resolved.durable_provenance())
    assert "api/download" not in repr(resolved)


@pytest.mark.asyncio
async def test_fractional_provider_size_is_only_a_nearest_byte_estimate() -> None:
    # This is the fractional size from Civitai's published API example. Its
    # binary float product is one fraction above the real integer byte count.
    size_kb = 2_546_414.971679688

    async def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/v1/model-versions/456":
            return httpx2.Response(
                200,
                json=version_payload(size_kb=size_kb),
                request=request,
            )
        return httpx2.Response(
            200,
            json=model_payload(version=version_payload(size_kb=size_kb)),
            request=request,
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
        client = CivitaiClient(api_token=TOKEN, http_client=http_client)
        resolved = await client.resolve_lora("https://civitai.com/models/123?modelVersionId=456")

    assert resolved.declared_size_bytes == 2_607_528_931


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "version", "message"),
    [
        (model_payload(minor=True), version_payload(), "minor"),
        (model_payload(), version_payload(minor=True), "minor"),
        (model_payload(commercial_use="Sell"), version_payload(), "commercial image"),
        (model_payload(model_type="Checkpoint"), version_payload(), "not a LoRA"),
        (model_payload(), version_payload(scan="Pending"), "scanned Safetensors"),
        (
            model_payload(version=version_payload(size_kb=1e308)),
            version_payload(size_kb=1e308),
            "scanned Safetensors",
        ),
    ],
)
async def test_resolver_rejects_minor_disallowed_license_type_and_unscanned_files(
    model: dict[str, Any],
    version: dict[str, Any],
    message: str,
) -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        payload = version if "model-versions" in request.url.path else model
        return httpx2.Response(200, json=payload, request=request)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
        client = CivitaiClient(http_client=http_client)
        with pytest.raises(CivitaiSourceSelectionError, match=message):
            await client.resolve_lora("https://civitai.com/models/123?modelVersionId=456")


def resolved_for_download(*, download_url: str) -> CivitaiResolvedLora:
    return CivitaiResolvedLora(
        model_id=123,
        version_id=456,
        file_id=789,
        model_type=CivitaiModelType.LORA,
        model_name="Creator LoRA",
        version_name="V1",
        target_filename="Creator-LoRA.safetensors",
        canonical_source_url="https://civitai.com/models/123?modelVersionId=456",
        creator="creator",
        base_model="SDXL 1.0",
        trained_words=("test-style",),
        declared_size_bytes=12,
        sha256=SHA256,
        scan=CivitaiFileScan(pickle_result="Success", virus_result="Success"),
        license_terms=CivitaiLicenseTerms(
            allow_no_credit=False,
            commercial_use=("Image",),
            allow_derivatives=True,
            allow_different_license=False,
        ),
        nsfw=True,
        nsfw_level=4,
        _download_url=download_url,
    )


@pytest.mark.asyncio
async def test_download_redirect_strips_bearer_and_never_exposes_signed_url() -> None:
    signed_url = (
        "https://b2.civitai.com/file/object?X-Amz-Credential=credential&X-Amz-Signature=signature"
    )
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.host == "civitai.com":
            return httpx2.Response(302, headers={"Location": signed_url}, request=request)
        return httpx2.Response(200, content=b"bounded body", request=request)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
        client = CivitaiClient(api_token=TOKEN, http_client=http_client)
        resolved = resolved_for_download(download_url="https://civitai.com/api/download/models/456")
        async with client.open_download(resolved) as chunks:
            body = b"".join([chunk async for chunk in chunks])

    assert body == b"bounded body"
    assert requests[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert "authorization" not in requests[1].headers
    assert TOKEN not in repr(client)
    assert signed_url not in repr(resolved)


@pytest.mark.asyncio
async def test_download_errors_are_bounded_and_redacted_after_signed_redirect() -> None:
    signature = "very-sensitive-signed-query"
    signed_url = f"https://private-bucket.s3.amazonaws.com/object?X-Amz-Signature={signature}"

    async def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "civitai.com":
            return httpx2.Response(302, headers={"Location": signed_url}, request=request)
        return httpx2.Response(500, text=f"leaked {TOKEN} {signature}", request=request)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
        client = CivitaiClient(api_token=TOKEN, http_client=http_client)
        resolved = resolved_for_download(download_url="https://civitai.com/api/download/models/456")
        with pytest.raises(CivitaiAPIError) as captured:
            async with client.open_download(resolved) as chunks:
                _ = b"".join([chunk async for chunk in chunks])

    rendered = repr(captured.value) + str(captured.value)
    assert TOKEN not in rendered
    assert signature not in rendered
    assert len(str(captured.value)) < 200


def test_redirect_allowlist_and_public_ip_policy_are_boundary_safe() -> None:
    assert sanitize_download_destination(
        "https://bucket.s3.amazonaws.com/file?X-Amz-Signature=opaque"
    ).startswith("https://bucket.s3.amazonaws.com/")
    for value in (
        "http://bucket.s3.amazonaws.com/file",
        "https://localhost/file",
        "https://evilamazonaws.com/file",
        "https://user:pass@bucket.s3.amazonaws.com/file",
    ):
        with pytest.raises(CivitaiURLValidationError):
            sanitize_download_destination(value)
    assert is_public_ip("8.8.8.8") is True
    for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "::1"):
        assert is_public_ip(address) is False
    with pytest.raises(CivitaiTransportError):
        validate_public_addresses(("8.8.8.8", "127.0.0.1"))


class _RecordingBackend:
    def __init__(self) -> None:
        self.hosts: list[str] = []

    async def connect_tcp(self, host: str, *args: object, **kwargs: object) -> object:
        del args, kwargs
        self.hosts.append(host)
        return object()

    async def sleep(self, seconds: float) -> None:
        del seconds


@pytest.mark.asyncio
async def test_network_backend_blocks_private_dns_and_pins_public_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _RecordingBackend()
    backend = PublicOnlyNetworkBackend(cast(httpcore2.AsyncNetworkBackend, recorder))

    async def private_dns(*args: object, **kwargs: object) -> list[tuple[object, ...]]:
        del args, kwargs
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(
        "gen_automation.integrations.civitai.transport.anyio.getaddrinfo",
        private_dns,
    )
    with pytest.raises(CivitaiTransportError):
        await backend.connect_tcp("attacker.example", 443)
    assert recorder.hosts == []

    async def public_dns(*args: object, **kwargs: object) -> list[tuple[object, ...]]:
        del args, kwargs
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr(
        "gen_automation.integrations.civitai.transport.anyio.getaddrinfo",
        public_dns,
    )
    await backend.connect_tcp("storage.example", 443)
    assert recorder.hosts == ["8.8.8.8"]
