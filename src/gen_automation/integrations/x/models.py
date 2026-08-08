from dataclasses import dataclass
from typing import cast

type JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | list[JSONValue] | dict[str, JSONValue]
type JSONObject = dict[str, JSONValue]


@dataclass(frozen=True)
class XUploadedMedia:
    """A static image uploaded to X."""

    id: str
    media_key: str
    expires_after_seconds: int
    size: int


@dataclass(frozen=True)
class XPost:
    """The documented subset returned after creating an X post."""

    id: str
    text: str


def as_json_object(value: object, context: str) -> JSONObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{context} must be a JSON object")
    return cast(JSONObject, value)


def required_object(data: JSONObject, key: str, context: str) -> JSONObject:
    if key not in data:
        raise ValueError(f"{context}.{key} is required")
    return as_json_object(data[key], f"{context}.{key}")


def required_str(data: JSONObject, key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context}.{key} must be a non-empty string")
    return value


def required_int(data: JSONObject, key: str, context: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{context}.{key} must be an integer")
    return value


def required_bool(data: JSONObject, key: str, context: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{context}.{key} must be a boolean")
    return value
