import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from gen_automation.domain.prompt_variables import (
    PROMPT_VARIABLE_FIELDS,
    PromptVariableResolver,
    decode_prompt_variables,
    validate_prompt_variables,
)
from gen_automation.services.new_sets import NewSetSubmission, resolve_new_set_prompt_variables


def test_variables_are_literal_case_sensitive_text_and_preserve_wildcards() -> None:
    resolver = PromptVariableResolver(
        {
            "Character": "traveler (series), silver hair",
            "original outfit": "blue jacket, __clothes__",
            "setting": "__backgrounds__",
            "optional": "",
        }
    )
    assert (
        resolver.expand("(*Character*:1.1), *original outfit*, *setting**optional*", label="Prompt")
        == "(traveler (series), silver hair:1.1), blue jacket, __clothes__, __backgrounds__"
    )
    assert resolver.expand("*setting*, *optional*", label="Prompt") == "__backgrounds__, "
    with pytest.raises(ValueError, match=r"unknown prompt variable \*character\*"):
        resolver.expand("*character*", label="Prompt")


def test_nested_variables_and_repeated_tokens_expand_without_mutating_the_map() -> None:
    variables = {"character": "traveler", "outfit": "blue jacket", "look": "*character*, *outfit*"}
    resolver = PromptVariableResolver(variables)
    assert resolver.expand("*look*, *outfit*", label="Prompt") == (
        "traveler, blue jacket, blue jacket"
    )
    assert variables["look"] == "*character*, *outfit*"
    variables["outfit"] = "red jacket"
    assert resolver.expand("*look*", label="Prompt") == "traveler, blue jacket"


@pytest.mark.parametrize("text", ["ordinary prompt, __poses__", "**bold**", "2 * 3", "é 🙂"])
def test_non_variable_text_is_unchanged(text: str) -> None:
    assert PromptVariableResolver({}).expand(text, label="Prompt") == text


@pytest.mark.parametrize(
    ("variables", "text", "message"),
    [
        ({}, "*missing*", "unknown prompt variable"),
        ({"a": "*a*"}, "*a*", "cycle"),
        ({"a": "*b*", "b": "*a*"}, "*a*", "cycle"),
        ({"a": "*missing*"}, "*a*", "unknown prompt variable"),
        ({"a": "x" * 20_000}, "prefix *a*", "exceeds 20000"),
        ({"a": "x" * 10_001}, "*a*, *a*", "exceeds 20000"),
        ({"a": "x"}, "*a* " * 257, "too many"),
    ],
)
def test_unsafe_or_mistyped_expansions_fail_early(
    variables: dict[str, str], text: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        PromptVariableResolver(variables).expand(text, label="Batch 2 prompt")


def test_nested_expansion_depth_is_bounded() -> None:
    variables = {f"v{i}": f"*v{i + 1}*" for i in range(8)}
    variables["v8"] = "value"
    with pytest.raises(ValueError, match="nested too deeply"):
        PromptVariableResolver(variables).expand("*v0*", label="Prompt")
    assert PromptVariableResolver(variables).expand("*v1*", label="Prompt") == "value"


@pytest.mark.parametrize(
    "value",
    [
        [],
        None,
        {"": "value"},
        {" name": "value"},
        {"name ": "value"},
        {"*name*": "value"},
        {"a/b": "value"},
        {"9name": "value"},
        {"a" * 65: "value"},
        {"name": 3},
        {"name": "\x00"},
        {"name": "x" * 20_001},
        {f"name{i}": "" for i in range(51)},
        {f"name{i}": "x" * 20_000 for i in range(6)},
    ],
)
def test_definitions_reject_invalid_names_nontext_and_unbounded_values(value: object) -> None:
    with pytest.raises(ValueError):
        validate_prompt_variables(value)


def test_empty_snippets_multiline_and_literal_template_syntax_are_supported() -> None:
    variables = {"name_2-alt": "line 1\nline 2, {{plain text}}", "optional": ""}
    assert decode_prompt_variables(json.dumps(variables)) == variables
    assert decode_prompt_variables("") == {}
    assert PromptVariableResolver(variables).expand("*name_2-alt*", label="Prompt") == (
        "line 1\nline 2, {{plain text}}"
    )


@pytest.mark.parametrize(
    "value",
    ['{"a":"first","a":"second"}', "[]", "null", "{bad", " " * 200_001],
    ids=["duplicate", "array", "null", "malformed", "oversized"],
)
def test_json_field_is_strict(value: str) -> None:
    with pytest.raises(ValueError):
        decode_prompt_variables(value)


def _command(**overrides: object) -> NewSetSubmission:
    return NewSetSubmission.model_validate(
        {
            "slug": "variables-test",
            "title": "Variables test",
            "subject_approval_id": UUID(int=1),
            "checkpoint_approval_id": UUID(int=2),
            "workflow_approval_id": UUID(int=3),
            "prompt": "*character*, *outfit*",
            "prompt_variables": {"character": "traveler", "outfit": "blue jacket"},
            "seed": 42,
            "width": 1024,
            "height": 1024,
            "steps": 30,
            "sampler": "euler",
            "scheduler": "normal",
            "outputs_per_job": 1,
            "planned_job_count": 1,
            **overrides,
        }
    )


def test_all_prompt_fields_resolve_without_rewriting_source_or_batch_inheritance() -> None:
    # Trio allows every regional prompt field; the resolver treats all of them
    # identically and leaves missing batch overrides as None.
    from gen_automation.domain.controlled_duo import TrioCompositionPreset

    command = _command(
        composition_mode="trio",
        duo_contract_version=3,
        secondary_subject_approval_id=UUID(int=4),
        tertiary_subject_approval_id=UUID(int=5),
        composition_preset_id=next(iter(TrioCompositionPreset)),
        **dict.fromkeys(PROMPT_VARIABLE_FIELDS, "*character*, *outfit*"),
        batches=[
            {"name": "Inherited", "image_count": 1, "prompt": "*character*"},
            {
                "name": "Overridden",
                "image_count": 1,
                **dict.fromkeys(PROMPT_VARIABLE_FIELDS, "*outfit*"),
            },
        ],
    )
    snapshot = command.model_dump()
    resolved = resolve_new_set_prompt_variables(command)
    assert command.model_dump() == snapshot
    assert not resolved.prompt_variables
    for field in PROMPT_VARIABLE_FIELDS:
        assert getattr(resolved, field) == "traveler, blue jacket"
        assert getattr(resolved.batches[1], field) == "blue jacket"
    assert resolved.batches[0].prompt == "traveler"
    assert resolved.batches[0].negative_prompt is None


def test_empty_required_prompts_cannot_sneak_past_validation_via_variables() -> None:
    command = _command(prompt="*optional*", prompt_variables={"optional": ""})
    with pytest.raises(ValidationError, match="prompt must not be blank"):
        resolve_new_set_prompt_variables(command)


def test_model_validates_variable_types_without_coercion() -> None:
    with pytest.raises(ValidationError, match="must be text"):
        _command(prompt_variables={"character": 42})
