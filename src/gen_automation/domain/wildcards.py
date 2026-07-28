from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gen_automation.domain.release_spec import Sha256, StrictModel, WildcardName

MAX_WILDCARD_ENTRIES = 2000
MAX_WILDCARD_ENTRY_CHARACTERS = 2000
MAX_WILDCARD_ENTRIES_BYTES = 512 * 1024
MAX_WILDCARD_LIBRARIES = 500
MAX_WILDCARD_VERSIONS_PER_RELEASE = 64
MAX_WILDCARD_NESTING = 8
MAX_WILDCARD_EXPANSIONS = 256


def validate_wildcard_entries(entries: list[str]) -> list[str]:
    if not entries or len(entries) > MAX_WILDCARD_ENTRIES:
        raise ValueError(
            f"wildcard entries must contain between 1 and {MAX_WILDCARD_ENTRIES} items"
        )
    total_bytes = 0
    for entry in entries:
        if not isinstance(entry, str):
            raise ValueError("wildcard entries must be strings")
        if not entry.strip():
            raise ValueError("wildcard entries cannot be blank")
        if len(entry) > MAX_WILDCARD_ENTRY_CHARACTERS:
            raise ValueError("a wildcard entry is too long")
        if "\x00" in entry or "\r" in entry or "\n" in entry:
            raise ValueError("wildcard entries must be single lines without NUL bytes")
        total_bytes += len(entry.encode("utf-8"))
        if total_bytes > MAX_WILDCARD_ENTRIES_BYTES:
            raise ValueError("wildcard entries exceed the library byte limit")
    return entries


class WildcardCreate(StrictModel):
    name: WildcardName
    entries: list[str] = Field(min_length=1, max_length=MAX_WILDCARD_ENTRIES)

    @field_validator("entries")
    @classmethod
    def validate_entries(cls, entries: list[str]) -> list[str]:
        return validate_wildcard_entries(entries)


class WildcardReplace(StrictModel):
    expected_version_no: int = Field(ge=1)
    entries: list[str] = Field(min_length=1, max_length=MAX_WILDCARD_ENTRIES)

    @field_validator("entries")
    @classmethod
    def validate_entries(cls, entries: list[str]) -> list[str]:
        return validate_wildcard_entries(entries)


class WildcardAppend(StrictModel):
    expected_version_no: int = Field(ge=1)
    entries: list[str] = Field(min_length=1, max_length=MAX_WILDCARD_ENTRIES)

    @field_validator("entries")
    @classmethod
    def validate_entries(cls, entries: list[str]) -> list[str]:
        return validate_wildcard_entries(entries)


class WildcardRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    current_version_no: int
    current_version_id: UUID
    entries: list[str]
    entries_sha256: Sha256
    created_at: datetime
    updated_at: datetime
