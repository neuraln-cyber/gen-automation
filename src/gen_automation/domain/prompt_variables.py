"""Bounded literal prompt snippets, expanded before versioned random wildcards.

This is deliberately not a template language: no expressions, paths, or code
are evaluated. Only *name* tokens are substituted with operator-supplied text.
"""

import json
import re
from collections.abc import Mapping

MAX_PROMPT_VARIABLES = 50
MAX_VARIABLE_CHARACTERS = 20_000
MAX_VARIABLE_TOTAL_CHARACTERS = 100_000
MAX_VARIABLE_DEPTH = 8
MAX_VARIABLE_SUBSTITUTIONS = 256
VARIABLE_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_ -]{0,63}")
VARIABLE_TOKEN_PATTERN = re.compile(r"\*\*[^*\r\n]+\*\*|\*([A-Za-z][A-Za-z0-9_ -]{0,63})\*")
PROMPT_VARIABLE_FIELDS = (
    "prompt",
    "negative_prompt",
    "detailer_prompt",
    "detailer_negative_prompt",
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
)


def validate_prompt_variables(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("Prompt variables must be a name/value object")
    if len(value) > MAX_PROMPT_VARIABLES:
        raise ValueError(f"Use at most {MAX_PROMPT_VARIABLES} prompt variables")
    result: dict[str, str] = {}
    total = 0
    for name, snippet in value.items():
        if (
            not isinstance(name, str)
            or name != name.strip()
            or VARIABLE_NAME_PATTERN.fullmatch(name) is None
        ):
            raise ValueError(
                "Variable names must start with a letter and use only letters, numbers, "
                "spaces, underscores or hyphens (maximum 64 characters)"
            )
        if not isinstance(snippet, str):
            raise ValueError(f"Value for *{name}* must be text")
        if len(snippet) > MAX_VARIABLE_CHARACTERS:
            raise ValueError(f"Value for *{name}* exceeds {MAX_VARIABLE_CHARACTERS} characters")
        if "\x00" in snippet:
            raise ValueError(f"Value for *{name}* contains a NUL character")
        total += len(snippet)
        if total > MAX_VARIABLE_TOTAL_CHARACTERS:
            raise ValueError("Prompt variable values exceed 100000 characters in total")
        result[name] = snippet
    return result


def decode_prompt_variables(value: str) -> dict[str, str]:
    """Read the optional browser field without silently accepting duplicate names."""
    if not value:
        return {}
    if len(value) > 200_000:
        raise ValueError("Prompt variables are too large")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        decoded: dict[str, object] = {}
        for key, item in pairs:
            if key in decoded:
                raise ValueError(f"Duplicate prompt variable: *{key}*")
            decoded[key] = item
        return decoded

    try:
        decoded = json.loads(value, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, RecursionError):
        raise ValueError("Prompt variables are not valid JSON") from None
    return validate_prompt_variables(decoded)


class PromptVariableResolver:
    def __init__(self, variables: Mapping[str, str]) -> None:
        self.variables = validate_prompt_variables(dict(variables))

    def expand(self, text: str, *, label: str) -> str:
        substitutions = 0

        def expand_fragment(fragment: str, stack: tuple[str, ...]) -> str:
            nonlocal substitutions
            parts: list[str] = []
            length = 0
            cursor = 0
            for match in VARIABLE_TOKEN_PATTERN.finditer(fragment):
                name = match.group(1)
                if name is None:
                    # Preserve existing Markdown-style **emphasis** as literal
                    # text, while still allowing adjacent *one**two* variables.
                    continue
                if name not in self.variables:
                    raise ValueError(f"{label}: unknown prompt variable *{name}*")
                if name in stack:
                    raise ValueError(f"{label}: prompt variable cycle involving *{name}*")
                if len(stack) >= MAX_VARIABLE_DEPTH:
                    raise ValueError(f"{label}: prompt variables are nested too deeply (maximum 8)")
                substitutions += 1
                if substitutions > MAX_VARIABLE_SUBSTITUTIONS:
                    raise ValueError(f"{label}: too many prompt variable substitutions")
                replacement = expand_fragment(self.variables[name], (*stack, name))
                prefix = fragment[cursor : match.start()]
                length += len(prefix) + len(replacement)
                if length > MAX_VARIABLE_CHARACTERS:
                    raise ValueError(f"{label}: expanded prompt exceeds 20000 characters")
                parts.extend((prefix, replacement))
                cursor = match.end()
            suffix = fragment[cursor:]
            if length + len(suffix) > MAX_VARIABLE_CHARACTERS:
                raise ValueError(f"{label}: expanded prompt exceeds 20000 characters")
            parts.append(suffix)
            return "".join(parts)

        return expand_fragment(text, ())
