from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from gen_automation.domain.release_spec import ReleaseCreate, ReleaseSpecification
from gen_automation.services.generation_details import (
    _composition_payload,
    _prompt_payload,
    _PromptResolutionV5,
    _resolution_matches_generation,
)
from gen_automation.services.wildcards import (
    FrozenWildcard,
    FrozenWildcardCatalog,
    _generation_wildcard_names,
    resolve_wildcard_prompts,
)
from tests.factories import valid_release_payload

_RESOLVED_PROMPT_FIELDS = (
    "prompt",
    "character_a_prompt",
    "character_b_prompt",
    "character_c_prompt",
    "character_a_pose_prompt",
    "character_b_pose_prompt",
    "character_c_pose_prompt",
    "character_a_negative_prompt",
    "character_b_negative_prompt",
    "character_c_negative_prompt",
    "interaction_prompt",
    "camera_prompt",
    "negative_prompt",
    "detailer_prompt",
    "detailer_negative_prompt",
)


def _trio_specification() -> ReleaseSpecification:
    payload = deepcopy(valid_release_payload())
    specification = payload["specification"]
    assert isinstance(specification, dict)
    subjects = specification["subjects"]
    workflow = specification["workflow"]
    generation = specification["generation"]
    assert isinstance(subjects, list)
    assert isinstance(workflow, dict)
    assert isinstance(generation, dict)

    for index in (2, 3):
        subject = deepcopy(subjects[0])
        subject.update(
            {
                "name": f"Approved Adult Character {index}",
                "canonical_source_url": f"https://example.com/adult-character-{index}",
            }
        )
        subjects.append(subject)
    workflow["capabilities"] = ["controlled_trio_v1"]
    generation.update(
        {
            "composition_mode": "trio",
            "duo_contract_version": 3,
            "composition_preset_id": "trio_flexible",
            "character_a_prompt": "__identity/a__",
            "character_b_prompt": "adult woman, indigo braid, ivory coat",
            "character_c_prompt": "__identity/c__",
            "character_a_pose_prompt": "__poses/a__",
            "character_b_pose_prompt": "standing behind A",
            "character_c_pose_prompt": "__poses/c__",
            "character_a_negative_prompt": "indigo braid, silver hair",
            "character_b_negative_prompt": "copper bob, silver hair",
            "character_c_negative_prompt": "__negatives/c__",
            "interaction_prompt": "__poses/group__",
            "camera_prompt": "__camera/group__",
        }
    )
    return ReleaseCreate.model_validate(payload).specification


def _wildcard(name: str, entry: str, digest_character: str) -> FrozenWildcard:
    return FrozenWildcard(
        library_id=uuid4(),
        version_id=uuid4(),
        name=name,
        version_no=1,
        entries=(entry,),
        entries_sha256=digest_character * 64,
    )


def test_controlled_trio_v5_expands_and_presents_every_character_prompt() -> None:
    specification = _trio_specification()
    catalog = FrozenWildcardCatalog(
        by_name={
            "identity/a": _wildcard("identity/a", "adult woman, copper bob, green jacket", "a"),
            "identity/c": _wildcard("identity/c", "adult woman, silver pixie cut, blue dress", "b"),
            "poses/a": _wildcard("poses/a", "kneeling with one hand raised", "c"),
            "poses/c": _wildcard("poses/c", "leaning toward B", "d"),
            "negatives/c": _wildcard("negatives/c", "copper bob, indigo braid", "e"),
            "poses/group": _wildcard("poses/group", "A holds B while C leans against B", "f"),
            "camera/group": _wildcard("camera/group", "wide three-person framing", "1"),
        }
    )

    assert _generation_wildcard_names(specification) == set(catalog.by_name)
    resolved = resolve_wildcard_prompts(
        specification,
        catalog,
        seed=specification.generation.seed,
    )

    assert resolved.character_c_prompt == "adult woman, silver pixie cut, blue dress"
    assert resolved.character_c_pose_prompt == "leaning toward B"
    assert resolved.character_c_negative_prompt == "copper bob, indigo braid"
    assert resolved.evidence["schema_version"] == 5
    evidence = _PromptResolutionV5.model_validate(resolved.evidence)
    resolved_generation = specification.generation.model_copy(
        update={field: getattr(resolved, field) for field in _RESOLVED_PROMPT_FIELDS}
    )
    assert _resolution_matches_generation(evidence, resolved_generation)

    prompts = _prompt_payload(resolved_generation, evidence)
    character_c = prompts["character_c"]
    character_c_pose = prompts["character_c_pose"]
    character_c_negative = prompts["character_c_negative"]
    interaction = prompts["interaction"]
    assert isinstance(character_c, dict)
    assert isinstance(character_c_pose, dict)
    assert isinstance(character_c_negative, dict)
    assert isinstance(interaction, dict)
    assert character_c["resolved"] == "adult woman, silver pixie cut, blue dress"
    assert character_c_pose["resolved"] == "leaning toward B"
    assert character_c_negative["resolved"] == "copper bob, indigo braid"
    assert interaction["resolved"] == "A holds B while C leans against B"
    assert _composition_payload(resolved_generation) == {
        "mode": "trio",
        "contract_version": 1,
        "preset_id": "trio_flexible",
        "isolation_mode": "balanced",
        "quality_mode": "standard",
    }
    assert {selection["field"] for selection in resolved.evidence["selections"]} == {
        "character_a_prompt",
        "character_c_prompt",
        "character_a_pose_prompt",
        "character_c_pose_prompt",
        "character_c_negative_prompt",
        "interaction_prompt",
        "camera_prompt",
    }


def test_controlled_trio_v5_rejects_tampering_and_is_required_without_pose_wildcards() -> None:
    specification = _trio_specification()
    catalog = FrozenWildcardCatalog(
        by_name={
            name: _wildcard(name, f"resolved {name}", f"{index:x}")
            for index, name in enumerate(
                sorted(_generation_wildcard_names(specification)),
                start=1,
            )
        }
    )
    resolved = resolve_wildcard_prompts(
        specification,
        catalog,
        seed=specification.generation.seed,
    )
    evidence = _PromptResolutionV5.model_validate(resolved.evidence)
    resolved_generation = specification.generation.model_copy(
        update={field: getattr(resolved, field) for field in _RESOLVED_PROMPT_FIELDS}
    )

    tampered_evidence = deepcopy(resolved.evidence)
    tampered_evidence["source_character_c_prompt"] = "different source identity"
    with pytest.raises(ValidationError, match="source digest mismatch"):
        _PromptResolutionV5.model_validate(tampered_evidence)

    tampered_generation = resolved_generation.model_copy(
        update={"character_c_pose_prompt": "different resolved pose"}
    )
    assert not _resolution_matches_generation(evidence, tampered_generation)

    literal_generation = specification.generation.model_copy(
        update={
            "character_a_prompt": "adult woman A",
            "character_c_prompt": "adult woman C",
            "character_a_pose_prompt": "standing",
            "character_c_pose_prompt": "seated",
            "character_c_negative_prompt": "identity bleed",
            "interaction_prompt": "three people facing each other",
            "camera_prompt": "wide shot",
        }
    )
    literal = resolve_wildcard_prompts(
        specification,
        FrozenWildcardCatalog(by_name={}),
        seed=literal_generation.seed,
        generation=literal_generation,
    )
    assert literal.evidence["schema_version"] == 5
    assert literal.evidence["selections"] == []
    _PromptResolutionV5.model_validate(literal.evidence)
