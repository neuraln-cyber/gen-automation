import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src/gen_automation/static/dashboard.js"
PARTIAL = ROOT / "src/gen_automation/templates/dashboard/_prompt_variables.html"


def _run_node(harness: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the prompt-variable runtime contract")
    result = subprocess.run(  # noqa: S603 - executable is resolved from PATH.
        [node], input=harness, text=True, capture_output=True, check=False, timeout=15
    )
    assert result.returncode == 0, result.stderr


def test_both_generation_forms_share_the_simple_variable_editor() -> None:
    include = '{% include "dashboard/_prompt_variables.html" %}'
    for filename in ("new_set.html", "experiment_new.html"):
        source = (PARTIAL.parent / filename).read_text(encoding="utf-8")
        assert include in source
    partial = PARTIAL.read_text(encoding="utf-8")
    for marker in (
        "data-prompt-variable-name",
        "data-prompt-variable-value",
        "data-prompt-variable-add",
        "data-prompt-variable-remove",
        "data-prompt-variable-insert",
        "data-prompt-variables-status",
    ):
        assert marker in partial
    assert 'name="prompt_variables" data-prompt-variables-data hidden' in partial
    assert "form_values.get('prompt_variables', '{}')" in partial
    assert "__outfits__" in partial
    assert "*character*" in partial
    assert "Names are case-sensitive" in partial
    assert "data-prompt-variable-value data-danbooru-autocomplete" in partial
    assert 'aria-live="polite"' in partial


def test_variable_controls_preserve_global_preset_and_draft_ownership() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    fields = source.split("const AUTOMATION_PRESET_FIELDS", 1)[1].split("const namedControl", 1)[0]
    assert '"prompt_variables"' in fields
    variant_fields = source.split("const profileNames = [", 1)[1].split("];", 1)[0]
    assert '"prompt_variables"' not in variant_fields
    assert 'name === "prompt_variables" ? (fields[name] ?? "{}")' in source
    assert "profile: collectAutomationProfile(form)" in source
    assert "const result = applyAutomationProfile(form, draft.profile)" in source
    assert "const result = applyAutomationProfile(form, preset.profile)" in source
    restore = source.index(
        'form.dispatchEvent(new CustomEvent("gen-automation:restore-prompt-variables"))'
    )
    events = source.index('form.querySelectorAll("input, textarea, select")', restore)
    assert restore < events
    assert source.rindex("initializePromptVariables();") < source.rindex(
        "initializeAutomationBuilder();"
    )


def test_variable_ui_rejects_invalid_input_before_queue_or_preset_save() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    editor = source.split("function initializePromptVariables()", 1)[1].split(
        "const CONTROLLED_DUO_PRESETS", 1
    )[0]
    assert "event.stopImmediatePropagation()" in editor
    assert "}, { capture: true })" in editor
    assert "setCustomValidity(entry.nameError)" in editor
    assert "setCustomValidity(entry.valueError)" in editor
    assert "Object.fromEntries(validated" in editor
    assert "list.replaceChildren()" in editor
    assert "lastPrompt?.isConnected && !lastPrompt.disabled" in editor
    assert "insertPromptToken(target, token)" in editor
    assert "innerHTML" not in editor
    assert "Fix prompt variables before saving this preset" in source
    autocomplete = (SCRIPT.parent / "tag_autocomplete.js").read_text(encoding="utf-8")
    assert "/[<>*]/u.test(fragment)" in autocomplete


def test_variable_validation_runtime_is_bounded_and_preserves_prompt_text() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    helpers = source[
        source.index("const promptVariableEntries") : source.index(
            "function initializePromptVariables()"
        )
    ]
    _run_node(
        'const assert = require("node:assert/strict");\n'
        + helpers
        + r"""
assert.deepEqual(promptVariableEntries('{"character":"name, red hair","outfit":"__outfits__"}'), [
  ["character", "name, red hair"], ["outfit", "__outfits__"],
]);
assert.deepEqual(promptVariableEntries(""), []);
for (const invalid of ["[]", "null", '"text"', '{"name":3}', "{bad"]) {
  assert.throws(() => promptVariableEntries(invalid));
}
assert.throws(() => promptVariableEntries(JSON.stringify(
  Object.fromEntries(Array.from({length: 51}, (_, i) => [`var${i}`, "x"])),
)));
const rows = validatePromptVariableRows([
  [" character ", "name, red hair"],
  ["Character", "Case-sensitive"],
  ["outfit", "__outfits__, *setting*"],
  ["setting", ""],
  ["", ""],
]);
assert(rows.every(row => !row.nameError && !row.valueError));
assert.equal(rows[0].name, "character");
assert.equal(rows[2].value, "__outfits__, *setting*");
assert.equal(rows[3].ignored, false);
assert.equal(rows[4].ignored, true);
assert(validatePromptVariableRows([["character", "a"], ["character ", "b"]])[1].nameError);
for (const badName of ["", "4name", "*name*", "a.b", "a/b", "a".repeat(65)]) {
  assert(validatePromptVariableRows([[badName, "value"]])[0].nameError);
}
for (const goodName of ["original outfit", "outfit_1", "setting-day", "a".repeat(64)]) {
  assert.equal(validatePromptVariableRows([[goodName, ""]])[0].nameError, "");
}
assert(validatePromptVariableRows([["outfit", "x".repeat(20001)]])[0].valueError);
const oversized = validatePromptVariableRows(
  Array.from({length: 6}, (_, i) => [`var${i}`, "x".repeat(20000)]),
);
assert.equal(oversized[4].valueError, "");
assert(oversized[5].valueError);
"""
    )


def test_variable_profiles_runtime_round_trip_and_old_profiles_clear_stale_values() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    shared = source[
        source.index("const AUTOMATION_PRESET_FIELDS") : source.index(
            "const CONTROLLED_DUO_PRESETS"
        )
    ]
    batches = source[
        source.index("const CONTROLLED_BATCH_OVERRIDE_FIELDS") : source.index(
            "const readStoredAutomationPresets"
        )
    ]
    profiles = source[
        source.index("const normalizeAutomationPresetBatchPlan") : source.index(
            "function initializeImageSettingsSummary()"
        )
    ]
    _run_node(
        r"""
const assert = require("node:assert/strict");
const events = [];
class HTMLInputElement {
  constructor(value = "") { this.value = value; }
  dispatchEvent(event) { events.push(event.type); }
}
class HTMLTextAreaElement extends HTMLInputElement {}
class HTMLSelectElement extends HTMLInputElement {}
const window = { dispatchEvent() {} };
const controls = {
  prompt: new HTMLTextAreaElement("*character*, *outfit*, *setting*"),
  negative_prompt: new HTMLTextAreaElement("*avoid*"),
  prompt_variables: new HTMLTextAreaElement(JSON.stringify({
    character: "new character", outfit: "__outfits__", setting: "city rooftop", avoid: "rain",
  })),
};
const form = {
  dataset: {},
  querySelector(selector) {
    return controls[selector.match(/^\[name="([^"\]]+)"\]$/)?.[1]] || null;
  },
  querySelectorAll(selector) {
    return selector === "input, textarea, select" ? Object.values(controls) : [];
  },
  dispatchEvent(event) { events.push(event.type); },
};
"""
        + shared
        + batches
        + profiles
        + r"""
const saved = collectAutomationProfile(form);
const batchPlan = normalizeAutomationPresetBatchPlan([{
  name: "Original outfit", image_count: 4,
  prompt: "*character*, *outfit*, *setting*, __poses__",
}]);
assert.equal(batchPlan[0].prompt, "*character*, *outfit*, *setting*, __poses__");
assert.equal(Object.hasOwn(batchPlan[0], "prompt_variables"), false);
const exported = JSON.parse(JSON.stringify({
  presets: [{ profile: saved, batch_plan: batchPlan }],
}));
const draft = JSON.parse(JSON.stringify({ profile: saved, batch_plan: batchPlan }));
controls.prompt_variables.value = '{"character":"stale character"}';
assert.equal(applyAutomationProfile(form, exported.presets[0].profile).applied, true);
assert.equal(controls.prompt_variables.value, saved.fields.prompt_variables);
assert.equal(controls.prompt.value, "*character*, *outfit*, *setting*");
assert(events.indexOf("gen-automation:restore-prompt-variables") < events.indexOf("input"));
controls.prompt_variables.value = "{}";
assert.equal(applyAutomationProfile(form, draft.profile).applied, true);
assert.equal(JSON.parse(controls.prompt_variables.value).outfit, "__outfits__");
assert.equal(applyAutomationProfile(form, { fields: { prompt: "older prompt" } }).applied, true);
assert.equal(controls.prompt_variables.value, "{}");
assert.equal(controls.prompt.value, "older prompt");
for (const bad of ["[]", "broken", { outfit: "text" }]) {
  const result = applyAutomationProfile(form, {
    fields: { prompt: "must not apply", prompt_variables: bad },
  });
  assert.equal(result.applied, false);
  assert.equal(controls.prompt.value, "older prompt");
}
"""
    )
