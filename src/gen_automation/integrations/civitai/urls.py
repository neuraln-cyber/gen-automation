"""Strict parsing for user-pasted Civitai model and version URLs."""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit

from gen_automation.integrations.civitai.errors import CivitaiURLValidationError
from gen_automation.integrations.civitai.models import CivitaiSourceKind, CivitaiSourceRef

MAX_CIVITAI_URL_LENGTH = 2_048
_MAX_PROVIDER_ID = 2**63 - 1
_CIVITAI_HOSTS = frozenset({"civitai.com", "www.civitai.com"})
_MODEL_PATH = re.compile(r"^/models/(?P<id>[1-9][0-9]{0,18})(?:/[A-Za-z0-9._~-]{1,200})?/?$")
_API_MODEL_PATH = re.compile(r"^/api/v1/models/(?P<id>[1-9][0-9]{0,18})/?$")
_VERSION_PATH = re.compile(r"^/(?:api/v1/)?model-versions/(?P<id>[1-9][0-9]{0,18})/?$")
_DOWNLOAD_PATH = re.compile(r"^/api/download/models/(?P<id>[1-9][0-9]{0,18})/?$")
_DOWNLOAD_QUERY_KEYS = frozenset({"type", "format", "size", "fp"})


def parse_civitai_url(value: str) -> CivitaiSourceRef:
    """Parse a canonical Civitai model page, version API URL, or download URL."""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_CIVITAI_URL_LENGTH
        or "\\" in value
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise CivitaiURLValidationError("Civitai URL must be trimmed visible text")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname.casefold() if parsed.hostname is not None else None
        port = parsed.port
    except ValueError:
        raise CivitaiURLValidationError("Civitai URL is malformed") from None
    if (
        parsed.scheme.casefold() != "https"
        or hostname not in _CIVITAI_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or "%" in parsed.path
    ):
        raise CivitaiURLValidationError("Civitai URL must use credential-free HTTPS on civitai.com")

    model_match = _MODEL_PATH.fullmatch(parsed.path) or _API_MODEL_PATH.fullmatch(parsed.path)
    if model_match is not None:
        model_id = _provider_id(model_match.group("id"))
        query = _query(parsed.query)
        if set(query) - {"modelVersionId"}:
            raise CivitaiURLValidationError("Civitai model URL has unsupported query parameters")
        raw_version = query.get("modelVersionId")
        version_id = _provider_id(raw_version) if raw_version is not None else None
        canonical_query = (
            f"?{urlencode({'modelVersionId': version_id})}" if version_id is not None else ""
        )
        return CivitaiSourceRef(
            kind=CivitaiSourceKind.MODEL,
            canonical_url=f"https://civitai.com/models/{model_id}{canonical_query}",
            model_id=model_id,
            version_id=version_id,
        )

    version_match = _VERSION_PATH.fullmatch(parsed.path)
    if version_match is not None:
        if parsed.query:
            raise CivitaiURLValidationError("Civitai version URL must not contain a query")
        version_id = _provider_id(version_match.group("id"))
        return CivitaiSourceRef(
            kind=CivitaiSourceKind.VERSION,
            canonical_url=f"https://civitai.com/api/v1/model-versions/{version_id}",
            version_id=version_id,
        )

    download_match = _DOWNLOAD_PATH.fullmatch(parsed.path)
    if download_match is not None:
        query = _query(parsed.query)
        if set(query) - _DOWNLOAD_QUERY_KEYS:
            raise CivitaiURLValidationError("Civitai download URL has unsupported query parameters")
        version_id = _provider_id(download_match.group("id"))
        return CivitaiSourceRef(
            kind=CivitaiSourceKind.DOWNLOAD,
            canonical_url=f"https://civitai.com/api/download/models/{version_id}",
            version_id=version_id,
        )

    raise CivitaiURLValidationError("Civitai URL is not a supported model or version URL")


def _query(raw_query: str) -> dict[str, str]:
    if not raw_query:
        return {}
    try:
        pairs = parse_qsl(raw_query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise CivitaiURLValidationError("Civitai URL query is malformed") from None
    values: dict[str, str] = {}
    for key, value in pairs:
        if not key or not value or key in values:
            raise CivitaiURLValidationError("Civitai URL query is malformed")
        values[key] = value
    return values


def _provider_id(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or value.startswith("0"):
        raise CivitaiURLValidationError("Civitai identifier is invalid")
    identifier = int(value)
    if not 1 <= identifier <= _MAX_PROVIDER_ID:
        raise CivitaiURLValidationError("Civitai identifier is invalid")
    return identifier
