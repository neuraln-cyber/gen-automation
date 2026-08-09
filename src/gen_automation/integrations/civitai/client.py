"""Authenticated, fail-closed Civitai metadata and LoRA download client."""

import json
import math
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import cast
from urllib.parse import urljoin, urlsplit

import httpx2

from gen_automation.integrations.civitai.errors import (
    CivitaiAPIError,
    CivitaiDownloadError,
    CivitaiError,
    CivitaiProtocolError,
    CivitaiRateLimitError,
    CivitaiSourceSelectionError,
    CivitaiTransportError,
)
from gen_automation.integrations.civitai.models import (
    CivitaiFileScan,
    CivitaiLicenseTerms,
    CivitaiLoraVersionChoice,
    CivitaiModelType,
    CivitaiResolvedLora,
    CivitaiSourceRef,
    JSONValue,
)
from gen_automation.integrations.civitai.transport import (
    DEFAULT_DOWNLOAD_HOST_SUFFIXES,
    PublicOnlyAsyncTransport,
    is_civitai_credential_host,
    sanitize_download_destination,
)
from gen_automation.integrations.civitai.urls import parse_civitai_url

MAX_MANAGED_LORA_BYTES = 4 * 1024 * 1024 * 1024
MAX_METADATA_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_METADATA_ITEMS = 20_000
MAX_METADATA_DEPTH = 32
MAX_DOWNLOAD_REDIRECTS = 10
_API_BASE = "https://civitai.com/api/v1"
_DOWNLOAD_PATH = re.compile(r"^/api/download/models/[1-9][0-9]{0,18}/?$")
_SAFE_FILENAME_CHARACTER = re.compile(r"[^A-Za-z0-9._ -]+")


class _SecretToken:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<redacted>"


@dataclass(frozen=True, slots=True)
class _FileCandidate:
    file_id: int
    name: str
    target_filename: str
    declared_size_bytes: int
    sha256: str
    primary: bool
    scan: CivitaiFileScan
    download_url: str


class CivitaiClient:
    """Resolve Civitai URLs and stream one authenticated, bounded LoRA file."""

    def __init__(
        self,
        *,
        api_token: str | None = None,
        http_client: httpx2.AsyncClient | None = None,
        request_timeout: httpx2.Timeout | float = 30.0,
        download_host_suffixes: tuple[str, ...] = DEFAULT_DOWNLOAD_HOST_SUFFIXES,
    ) -> None:
        if api_token is not None and (
            not isinstance(api_token, str)
            or not api_token
            or api_token != api_token.strip()
            or len(api_token) > 4_096
            or any(ord(character) < 33 or ord(character) == 127 for character in api_token)
        ):
            raise ValueError("Civitai API token must be nonempty trimmed visible text")
        if not download_host_suffixes:
            raise ValueError("at least one Civitai download host suffix is required")
        normalized_suffixes = tuple(
            suffix.rstrip(".").casefold() for suffix in download_host_suffixes
        )
        if any(
            not suffix or "/" in suffix or ":" in suffix or suffix.startswith(".")
            for suffix in normalized_suffixes
        ):
            raise ValueError("Civitai download host suffix is invalid")
        self._api_token = _SecretToken(api_token) if api_token is not None else None
        self._owns_client = http_client is None
        self._http_client = http_client or httpx2.AsyncClient(
            transport=PublicOnlyAsyncTransport(),
            timeout=request_timeout,
            follow_redirects=False,
            trust_env=False,
        )
        self._download_host_suffixes = normalized_suffixes

    def __repr__(self) -> str:
        return f"CivitaiClient(authenticated={self._api_token is not None})"

    async def close(self) -> None:
        if self._owns_client:
            await self._http_client.aclose()

    async def resolve_lora(
        self,
        source: str | CivitaiSourceRef,
        *,
        version_id: int | None = None,
    ) -> CivitaiResolvedLora:
        reference = parse_civitai_url(source) if isinstance(source, str) else source
        selected_version_id = _selected_version(reference, version_id)

        if reference.model_id is not None and selected_version_id is None:
            raise CivitaiSourceSelectionError(
                "Civitai model URL requires an explicit model version selection"
            )

        if selected_version_id is None:
            raise CivitaiSourceSelectionError("Civitai source does not identify a version")
        version_data = await self._get_json(f"model-versions/{selected_version_id}")
        model_id = _positive_int(version_data.get("modelId"), "Civitai version.modelId")
        if reference.model_id is not None and model_id != reference.model_id:
            raise CivitaiSourceSelectionError("Civitai version does not belong to the model URL")
        model_data = await self._get_json(f"models/{model_id}")
        return _resolve_from_payloads(model_data, version_data)

    async def list_lora_versions(
        self,
        source: str | CivitaiSourceRef,
    ) -> tuple[CivitaiLoraVersionChoice, ...]:
        reference = parse_civitai_url(source) if isinstance(source, str) else source
        if reference.model_id is None:
            raise CivitaiSourceSelectionError("version choices require a Civitai model URL")
        model_data = await self._get_json(f"models/{reference.model_id}")
        versions = _object_list(model_data, "modelVersions", "Civitai model", maximum=1_000)
        choices: list[CivitaiLoraVersionChoice] = []
        for raw_version in versions:
            try:
                resolved = _resolve_from_payloads(model_data, raw_version)
            except CivitaiSourceSelectionError:
                continue
            choices.append(
                CivitaiLoraVersionChoice(
                    version_id=resolved.version_id,
                    name=resolved.version_name,
                    base_model=resolved.base_model,
                    target_filename=resolved.target_filename,
                    declared_size_bytes=resolved.declared_size_bytes,
                    sha256=resolved.sha256,
                )
            )
        if not choices:
            raise CivitaiSourceSelectionError(
                "Civitai model has no downloadable scanned Safetensors LoRA versions"
            )
        return tuple(choices)

    @asynccontextmanager
    async def open_download(
        self,
        resolved: CivitaiResolvedLora,
        *,
        max_bytes: int = MAX_MANAGED_LORA_BYTES,
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        """Yield response chunks while keeping redirects and credentials inside this adapter."""

        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        current_url = resolved._download_url
        for redirect_count in range(MAX_DOWNLOAD_REDIRECTS + 1):
            headers = self._headers(current_url, accept="application/octet-stream")
            headers["Accept-Encoding"] = "identity"
            try:
                async with self._http_client.stream(
                    "GET",
                    current_url,
                    headers=headers,
                    follow_redirects=False,
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if redirect_count == MAX_DOWNLOAD_REDIRECTS:
                            raise CivitaiDownloadError("Civitai download exceeded redirect limit")
                        location = response.headers.get("location")
                        if location is None:
                            raise CivitaiDownloadError(
                                "Civitai download redirect omitted its destination"
                            )
                        current_url = sanitize_download_destination(
                            urljoin(current_url, location),
                            allowed_host_suffixes=self._download_host_suffixes,
                        )
                        continue
                    if response.status_code != 200:
                        raise _api_error(response)
                    content_encoding = response.headers.get("content-encoding", "identity")
                    if content_encoding.casefold() not in {"", "identity"}:
                        raise CivitaiDownloadError(
                            "Civitai download used an unsupported content encoding"
                        )
                    content_length = _content_length(response)
                    if content_length is not None and content_length > max_bytes:
                        raise CivitaiDownloadError("Civitai LoRA exceeds the managed size limit")
                    yield _bounded_response_chunks(response, max_bytes=max_bytes)
                    return
            except (CivitaiError, GeneratorExit):
                raise
            except (httpx2.TimeoutException, httpx2.RequestError):
                raise CivitaiTransportError("Civitai download transport failed") from None
        raise CivitaiDownloadError("Civitai download exceeded redirect limit")

    async def _get_json(self, path: str) -> dict[str, JSONValue]:
        url = f"{_API_BASE}/{path}"
        try:
            async with self._http_client.stream(
                "GET",
                url,
                headers=self._headers(url, accept="application/json"),
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise _api_error(response)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_METADATA_RESPONSE_BYTES:
                        raise CivitaiProtocolError("Civitai metadata response is too large")
        except CivitaiError:
            raise
        except (httpx2.TimeoutException, httpx2.RequestError):
            raise CivitaiTransportError("Civitai metadata transport failed") from None
        try:
            value = json.loads(
                body,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
            _validate_json_shape(value, depth=0, counter=[0])
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
            raise CivitaiProtocolError("Civitai metadata response is invalid JSON") from None
        return _object(value, "Civitai metadata")

    def _headers(self, url: str, *, accept: str) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": "gen-automation-civitai/1",
        }
        hostname = urlsplit(url).hostname
        if (
            self._api_token is not None
            and hostname is not None
            and is_civitai_credential_host(hostname)
        ):
            headers["Authorization"] = f"Bearer {self._api_token.reveal()}"
        return headers


async def _bounded_response_chunks(
    response: httpx2.Response,
    *,
    max_bytes: int,
) -> AsyncIterator[bytes]:
    total = 0
    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise CivitaiDownloadError("Civitai LoRA exceeds the managed size limit")
        yield bytes(chunk)
    if total == 0:
        raise CivitaiDownloadError("Civitai download returned an empty body")


def _selected_version(reference: CivitaiSourceRef, requested: int | None) -> int | None:
    if requested is not None and (
        isinstance(requested, bool) or not isinstance(requested, int) or requested <= 0
    ):
        raise ValueError("Civitai version id must be a positive integer")
    if reference.version_id is not None and requested is not None:
        if reference.version_id != requested:
            raise CivitaiSourceSelectionError("requested Civitai version conflicts with the URL")
    return reference.version_id if reference.version_id is not None else requested


def _resolve_from_payloads(
    model_data: dict[str, JSONValue],
    version_data: dict[str, JSONValue],
) -> CivitaiResolvedLora:
    model_id = _positive_int(model_data.get("id"), "Civitai model.id")
    version_model_id = _positive_int(version_data.get("modelId"), "Civitai version.modelId")
    if version_model_id != model_id:
        raise CivitaiProtocolError("Civitai model/version relationship is inconsistent")
    model_type = _model_type(model_data.get("type"))
    _reject_unavailable(model_data, "Civitai model")
    _reject_unavailable(version_data, "Civitai version")
    _reject_minor_content(model_data, "Civitai model")
    _reject_minor_content(version_data, "Civitai version")
    license_terms = _license_terms(model_data)
    if not any(value.casefold() == "image" for value in license_terms.commercial_use):
        raise CivitaiSourceSelectionError("Civitai model does not permit commercial image use")
    candidate = _select_file(version_data)
    model_name = _text(model_data.get("name"), "Civitai model.name", maximum=300)
    version_name = _text(version_data.get("name"), "Civitai version.name", maximum=300)
    creator_data = model_data.get("creator")
    creator = None
    if creator_data is not None:
        creator = _optional_text(
            _object(creator_data, "Civitai model.creator").get("username"),
            "Civitai model.creator.username",
            maximum=200,
        )
    trained_words = tuple(
        _text(value, "Civitai version.trainedWords", maximum=200)
        for value in _value_list(
            version_data.get("trainedWords", []),
            "Civitai version.trainedWords",
            maximum=100,
        )
    )
    if len({word.casefold() for word in trained_words}) != len(trained_words):
        raise CivitaiProtocolError("Civitai trained words contain duplicates")
    nsfw = _optional_bool(model_data.get("nsfw"), "Civitai model.nsfw", default=False)
    nsfw_level_value = model_data.get("nsfwLevel")
    nsfw_level = (
        _nonnegative_int(nsfw_level_value, "Civitai model.nsfwLevel")
        if nsfw_level_value is not None
        else None
    )
    version_id = _positive_int(version_data.get("id"), "Civitai version.id")
    return CivitaiResolvedLora(
        model_id=model_id,
        version_id=version_id,
        file_id=candidate.file_id,
        model_type=model_type,
        model_name=model_name,
        version_name=version_name,
        target_filename=candidate.target_filename,
        canonical_source_url=(f"https://civitai.com/models/{model_id}?modelVersionId={version_id}"),
        creator=creator,
        base_model=_optional_text(
            version_data.get("baseModel"),
            "Civitai version.baseModel",
            maximum=200,
        ),
        trained_words=trained_words,
        declared_size_bytes=candidate.declared_size_bytes,
        sha256=candidate.sha256,
        scan=candidate.scan,
        license_terms=license_terms,
        nsfw=nsfw,
        nsfw_level=nsfw_level,
        _download_url=candidate.download_url,
    )


def _model_type(value: JSONValue | None) -> CivitaiModelType:
    raw = _text(value, "Civitai model.type", maximum=32)
    mapping = {item.value.casefold(): item for item in CivitaiModelType}
    model_type = mapping.get(raw.casefold())
    if model_type is None:
        raise CivitaiSourceSelectionError("Civitai model is not a LoRA, LoCon, or DoRA")
    return model_type


def _select_file(version_data: dict[str, JSONValue]) -> _FileCandidate:
    files = _object_list(version_data, "files", "Civitai version", maximum=200)
    candidates: list[_FileCandidate] = []
    for data in files:
        if _optional_text(data.get("type"), "Civitai file.type", maximum=40) != "Model":
            continue
        name = _text(data.get("name"), "Civitai file.name", maximum=500)
        if not name.casefold().endswith(".safetensors"):
            continue
        metadata = _object(data.get("metadata"), "Civitai file.metadata")
        file_format = _optional_text(
            metadata.get("format"),
            "Civitai file.metadata.format",
            maximum=40,
        )
        if file_format is None or file_format.casefold().replace(" ", "") not in {
            "safetensor",
            "safetensors",
        }:
            continue
        scan = CivitaiFileScan(
            pickle_result=_text(
                data.get("pickleScanResult"),
                "Civitai file.pickleScanResult",
                maximum=80,
            ),
            virus_result=_text(
                data.get("virusScanResult"),
                "Civitai file.virusScanResult",
                maximum=80,
            ),
        )
        if scan.pickle_result.casefold() != "success" or scan.virus_result.casefold() != "success":
            continue
        size_kb = data.get("sizeKB")
        if (
            isinstance(size_kb, bool)
            or not isinstance(size_kb, int | float)
            or not math.isfinite(size_kb)
            or size_kb <= 0
        ):
            raise CivitaiProtocolError("Civitai file.sizeKB must be a positive number")
        if size_kb > MAX_MANAGED_LORA_BYTES / 1024:
            continue
        # Civitai exposes sizeKB as display metadata, sometimes with a binary
        # floating-point tail. Keep the nearest-byte estimate for UI/capacity
        # preflight, but the import runtime verifies the actual streamed size
        # and SHA-256 instead of treating this estimate as an immutable length.
        declared_size_bytes = max(1, math.floor((size_kb * 1024) + 0.5))
        if declared_size_bytes > MAX_MANAGED_LORA_BYTES:
            continue
        hashes = _object(data.get("hashes"), "Civitai file.hashes")
        sha256 = _sha256(hashes.get("SHA256", hashes.get("sha256")))
        download_url = _provider_download_url(
            _text(data.get("downloadUrl"), "Civitai file.downloadUrl", maximum=2_048)
        )
        candidates.append(
            _FileCandidate(
                file_id=_positive_int(data.get("id"), "Civitai file.id"),
                name=name,
                target_filename=_target_filename(name),
                declared_size_bytes=declared_size_bytes,
                sha256=sha256,
                primary=_optional_bool(data.get("primary"), "Civitai file.primary", default=False),
                scan=scan,
                download_url=download_url,
            )
        )
    primary = [candidate for candidate in candidates if candidate.primary]
    if len(primary) == 1:
        return primary[0]
    if not primary and len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise CivitaiSourceSelectionError(
            "Civitai version has no scanned Safetensors model file within 4 GiB"
        )
    raise CivitaiSourceSelectionError("Civitai version does not identify one primary model file")


def _provider_download_url(value: str) -> str:
    sanitized = sanitize_download_destination(value)
    parsed = urlsplit(sanitized)
    if not is_civitai_credential_host(parsed.hostname or "") or not _DOWNLOAD_PATH.fullmatch(
        parsed.path
    ):
        raise CivitaiProtocolError("Civitai file download URL is not a provider download endpoint")
    parse_civitai_url(sanitized)
    return sanitized


def _target_filename(value: str) -> str:
    if "\\" in value:
        raise CivitaiProtocolError("Civitai file name is invalid")
    basename = PurePosixPath(value).name
    if basename != value or basename in {"", ".", ".."}:
        raise CivitaiProtocolError("Civitai file name is invalid")
    stem = basename[: -len(".safetensors")]
    sanitized = re.sub(r"[ ._-]+", "-", _SAFE_FILENAME_CHARACTER.sub("-", stem)).strip("-.")
    if not sanitized:
        sanitized = "civitai-lora"
    return f"{sanitized[:200]}.safetensors"


def _license_terms(model_data: dict[str, JSONValue]) -> CivitaiLicenseTerms:
    commercial_raw = model_data.get("allowCommercialUse", "None")
    commercial_values: tuple[str, ...]
    if isinstance(commercial_raw, str):
        commercial_values = (commercial_raw,)
    elif isinstance(commercial_raw, list):
        if len(commercial_raw) > 16:
            raise CivitaiProtocolError("Civitai commercial-use terms are too large")
        commercial_values = tuple(
            _text(value, "Civitai allowCommercialUse", maximum=80) for value in commercial_raw
        )
    else:
        raise CivitaiProtocolError("Civitai allowCommercialUse is invalid")
    return CivitaiLicenseTerms(
        allow_no_credit=_optional_bool(
            model_data.get("allowNoCredit"),
            "Civitai model.allowNoCredit",
            default=False,
        ),
        commercial_use=commercial_values,
        allow_derivatives=_optional_bool(
            model_data.get("allowDerivatives"),
            "Civitai model.allowDerivatives",
            default=False,
        ),
        allow_different_license=_optional_bool(
            model_data.get("allowDifferentLicense"),
            "Civitai model.allowDifferentLicense",
            default=False,
        ),
    )


def _reject_unavailable(data: dict[str, JSONValue], context: str) -> None:
    for key in ("mode", "status"):
        value = data.get(key)
        if isinstance(value, str) and value.casefold() in {
            "archived",
            "taken down",
            "takedown",
            "deleted",
        }:
            raise CivitaiSourceSelectionError(f"{context} is unavailable")


def _reject_minor_content(data: dict[str, JSONValue], context: str) -> None:
    for key in ("minor", "isMinor", "minorContent", "isMinorContent"):
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, bool):
            raise CivitaiProtocolError(f"{context}.{key} must be a boolean")
        if value:
            raise CivitaiSourceSelectionError("Civitai source is marked as depicting minor content")


def _api_error(response: httpx2.Response) -> CivitaiAPIError:
    status = response.status_code
    messages = {
        400: "request was rejected",
        401: "authentication was rejected",
        403: "download is not permitted",
        404: "model or version was not found",
    }
    message = messages.get(status, "provider request failed")
    if status == 429:
        return CivitaiRateLimitError(
            message="request rate limit was exceeded",
            retry_after_seconds=_retry_after(response.headers.get("retry-after")),
        )
    return CivitaiAPIError(status_code=status, message=message)


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _content_length(response: httpx2.Response) -> int | None:
    value = response.headers.get("content-length")
    if value is None:
        return None
    if not value.isascii() or not value.isdecimal():
        raise CivitaiDownloadError("Civitai download Content-Length is invalid")
    return int(value)


def _unique_object(pairs: list[tuple[str, JSONValue]]) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _validate_json_shape(value: object, *, depth: int, counter: list[int]) -> None:
    if depth > MAX_METADATA_DEPTH:
        raise ValueError("JSON nesting is too deep")
    counter[0] += 1
    if counter[0] > MAX_METADATA_ITEMS:
        raise ValueError("JSON has too many items")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 500:
                raise ValueError("JSON object key is invalid")
            _validate_json_shape(child, depth=depth + 1, counter=counter)
    elif isinstance(value, list):
        for child in value:
            _validate_json_shape(child, depth=depth + 1, counter=counter)
    elif not isinstance(value, str | int | float | bool | None):
        raise ValueError("JSON value is invalid")


def _object(value: JSONValue | object, context: str) -> dict[str, JSONValue]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CivitaiProtocolError(f"{context} must be an object")
    return cast(dict[str, JSONValue], value)


def _object_list(
    data: dict[str, JSONValue],
    key: str,
    context: str,
    *,
    maximum: int,
) -> tuple[dict[str, JSONValue], ...]:
    values = _value_list(data.get(key), f"{context}.{key}", maximum=maximum)
    return tuple(_object(value, f"{context}.{key}") for value in values)


def _value_list(value: JSONValue | None, context: str, *, maximum: int) -> list[JSONValue]:
    if not isinstance(value, list) or len(value) > maximum:
        raise CivitaiProtocolError(f"{context} must be a bounded array")
    return value


def _text(value: JSONValue | None, context: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise CivitaiProtocolError(f"{context} must be trimmed visible text")
    return value


def _optional_text(value: JSONValue | None, context: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, context, maximum=maximum)


def _positive_int(value: JSONValue | None, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > 2**63 - 1:
        raise CivitaiProtocolError(f"{context} must be a positive integer")
    return value


def _nonnegative_int(value: JSONValue | None, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CivitaiProtocolError(f"{context} must be a non-negative integer")
    return value


def _optional_bool(value: JSONValue | None, context: str, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise CivitaiProtocolError(f"{context} must be a boolean")
    return value


def _sha256(value: JSONValue | None) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise CivitaiProtocolError("Civitai file SHA-256 is missing or invalid")
    return value.casefold()
