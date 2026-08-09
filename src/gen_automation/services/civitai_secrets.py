"""Fail-closed Civitai API-key loading without durable token projection."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, cast

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from gen_automation.config import Settings

_REFERENCE_PREFIX = "aws-secrets-manager://"


class CivitaiCredentialError(RuntimeError):
    """A redacted credential error safe for application startup logs."""


async def load_civitai_api_key(settings: Settings) -> str | None:
    if not settings.lora_manager_enabled:
        return None
    if settings.civitai_api_key is not None:
        return _validate_key(settings.civitai_api_key.get_secret_value())
    reference = settings.civitai_api_secret_reference
    if reference is None or not reference.startswith(_REFERENCE_PREFIX):
        raise CivitaiCredentialError("Civitai credential configuration is unavailable")
    arn = reference.removeprefix(_REFERENCE_PREFIX)
    region = _arn_region(arn)
    client: Any = boto3.client(
        "secretsmanager",
        region_name=region,
        config=Config(
            connect_timeout=5,
            read_timeout=15,
            retries={"mode": "standard", "max_attempts": 3},
            max_pool_connections=1,
            user_agent_extra="gen-automation-civitai/1",
        ),
    )
    try:
        response = await asyncio.to_thread(client.get_secret_value, SecretId=arn)
    except (BotoCoreError, ClientError):
        raise CivitaiCredentialError("Civitai credential could not be loaded") from None
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            await asyncio.to_thread(close)
    secret = response.get("SecretString")
    if not isinstance(secret, str):
        raise CivitaiCredentialError("Civitai credential has an invalid format")
    return _secret_value(secret)


def _secret_value(raw: str) -> str:
    try:
        parsed = json.loads(raw, object_pairs_hook=_unique_object)
    except json.JSONDecodeError:
        if raw.lstrip().startswith(("{", "[", '"')):
            raise CivitaiCredentialError("Civitai credential has an invalid format") from None
        return _validate_key(raw)
    except ValueError:
        raise CivitaiCredentialError("Civitai credential has an invalid format") from None
    if isinstance(parsed, str):
        return _validate_key(parsed)
    if not isinstance(parsed, Mapping):
        raise CivitaiCredentialError("Civitai credential has an invalid format")
    if set(parsed) == {"api_key"}:
        return _validate_key(cast(str, parsed["api_key"]))
    if set(parsed) == {"schema", "api_token"} and parsed["schema"] == ("gen-automation/civitai/v1"):
        return _validate_key(cast(str, parsed["api_token"]))
    raise CivitaiCredentialError("Civitai credential has an invalid format")


def _validate_key(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 4096
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise CivitaiCredentialError("Civitai credential has an invalid format")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _arn_region(arn: str) -> str:
    # Parse the ARN structurally without accepting a URL-like or empty region.
    parts = arn.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn" or parts[2] != "secretsmanager":
        raise CivitaiCredentialError("Civitai credential reference is invalid")
    region = parts[3]
    if not region or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in region
    ):
        raise CivitaiCredentialError("Civitai credential reference is invalid")
    return region
