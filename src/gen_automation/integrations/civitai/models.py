"""Bounded, credential-free Civitai metadata used by LoRA onboarding."""

from dataclasses import dataclass, field
from enum import StrEnum

type JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | list[JSONValue] | dict[str, JSONValue]


class CivitaiSourceKind(StrEnum):
    MODEL = "model"
    VERSION = "version"
    DOWNLOAD = "download"


class CivitaiModelType(StrEnum):
    LORA = "LORA"
    LOCON = "LoCon"
    DORA = "DoRA"


@dataclass(frozen=True, slots=True)
class CivitaiSourceRef:
    kind: CivitaiSourceKind
    canonical_url: str
    model_id: int | None = None
    version_id: int | None = None


@dataclass(frozen=True, slots=True)
class CivitaiLicenseTerms:
    allow_no_credit: bool
    commercial_use: tuple[str, ...]
    allow_derivatives: bool
    allow_different_license: bool

    @property
    def permits_commercial_images(self) -> bool:
        """Whether Civitai explicitly reports commercial generated-image use."""

        return any(value.casefold() == "image" for value in self.commercial_use)

    def as_json(self) -> dict[str, JSONValue]:
        return {
            "allow_no_credit": self.allow_no_credit,
            "commercial_use": list(self.commercial_use),
            "allow_derivatives": self.allow_derivatives,
            "allow_different_license": self.allow_different_license,
        }


@dataclass(frozen=True, slots=True)
class CivitaiFileScan:
    pickle_result: str
    virus_result: str

    def as_json(self) -> dict[str, JSONValue]:
        return {
            "pickle_result": self.pickle_result,
            "virus_result": self.virus_result,
        }


@dataclass(frozen=True, slots=True)
class CivitaiLoraVersionChoice:
    version_id: int
    name: str
    base_model: str | None
    target_filename: str
    declared_size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CivitaiLoraVersionListing:
    versions: tuple[CivitaiLoraVersionChoice, ...]
    license_terms: CivitaiLicenseTerms


@dataclass(frozen=True, slots=True)
class CivitaiResolvedLora:
    model_id: int
    version_id: int
    file_id: int
    model_type: CivitaiModelType
    model_name: str
    version_name: str
    target_filename: str
    canonical_source_url: str
    creator: str | None
    base_model: str | None
    trained_words: tuple[str, ...]
    declared_size_bytes: int
    sha256: str
    scan: CivitaiFileScan
    license_terms: CivitaiLicenseTerms
    nsfw: bool
    nsfw_level: int | None
    _download_url: str = field(repr=False)

    def durable_provenance(self) -> dict[str, JSONValue]:
        """Return only bounded, credential-free facts suitable for persistence."""

        return {
            "provider": "civitai",
            "source_url": self.canonical_source_url,
            "model_id": self.model_id,
            "version_id": self.version_id,
            "file_id": self.file_id,
            "model_type": self.model_type.value,
            "model_name": self.model_name,
            "version_name": self.version_name,
            "creator": self.creator,
            "base_model": self.base_model,
            "trained_words": list(self.trained_words),
            "declared_size_bytes": self.declared_size_bytes,
            "sha256": self.sha256,
            "scan": self.scan.as_json(),
            "license_terms": self.license_terms.as_json(),
            "nsfw": self.nsfw,
            "nsfw_level": self.nsfw_level,
        }
