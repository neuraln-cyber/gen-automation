from pathlib import Path

ROOT = Path(__file__).parents[1]
NEW_SET = ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "new_set.html"
EXPERIMENT = ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "experiment_new.html"
CONTROLS = (
    ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "_controlled_duo_controls.html"
)
SCRIPT = ROOT / "src" / "gen_automation" / "static" / "dashboard.js"
STYLES = ROOT / "src" / "gen_automation" / "static" / "dashboard_ux.css"
WILDCARDS = ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "wildcards.html"


def test_controlled_duo_builder_is_shared_and_exposes_the_complete_contract() -> None:
    new_set = NEW_SET.read_text(encoding="utf-8")
    experiment = EXPERIMENT.read_text(encoding="utf-8")
    controls = CONTROLS.read_text(encoding="utf-8")

    include = '{% include "dashboard/_controlled_duo_controls.html" %}'
    assert include in new_set
    assert include in experiment
    for field in (
        "duo_contract_version",
        "composition_preset_id",
        "interaction_prompt",
        "camera_prompt",
        "duo_isolation_mode",
        "duo_quality_mode",
    ):
        assert f'name="{field}"' in controls
    for field in (
        "character_a_negative_prompt",
        "character_b_negative_prompt",
        "character_a_pose_prompt",
        "character_b_pose_prompt",
        "character_c_prompt",
        "character_c_negative_prompt",
        "character_c_pose_prompt",
    ):
        assert f'name="{field}"' in new_set
        if field in {"character_a_negative_prompt", "character_b_negative_prompt"}:
            assert f'name="{field}"' in experiment
    assert "Combined pose / interaction" in controls
    assert 'data-prompt-wildcard-target="interaction_prompt"' in controls
    assert 'data-prompt-wildcard-target="camera_prompt"' in controls
    assert 'data-prompt-wildcard-target="character_a_pose_prompt"' in new_set
    assert 'data-prompt-wildcard-target="character_b_pose_prompt"' in new_set
    assert 'data-prompt-wildcard-target="character_c_pose_prompt"' in new_set
    assert "supports_controlled_trio_v1" in new_set
    assert "data-trio-contract-v1" in new_set


def test_controlled_duo_builder_has_flexible_and_six_guides_with_mask_preflight() -> None:
    controls = CONTROLS.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")

    for preset in (
        "flexible",
        "close_portrait",
        "overhead",
        "low_angle",
        "diagonal_depth",
        "back_to_back",
        "full_body",
    ):
        assert f'value="{preset}"' in controls
        assert f"{preset}:" in script
    assert "data-duo-mask-preview" in controls
    assert "Auto / flexible" in controls
    assert "They never limit actions or poses" in controls
    assert "identity-region guides, not pose choices" in controls
    assert "Three guided identity regions" in script
    assert "Pair preflight" in controls
    assert "Possible prompt bleed" in script
    assert 'mode === "trio" ? "exact-three" : "exact-two"' in script
    assert "form.querySelectorAll('[data-batch-field=\"prompt\"]')" in script
    assert '.controlled-duo-mask-preview[data-preset="diagonal_depth"]' in styles


def test_controlled_duo_capabilities_fail_closed_and_costs_are_honest() -> None:
    controls = CONTROLS.read_text(encoding="utf-8")
    new_set = NEW_SET.read_text(encoding="utf-8")
    experiment = EXPERIMENT.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")

    for template in (new_set, experiment):
        assert "data-duo-contract-v2=" in template
        assert "data-duo-strict-isolation=" in template
        assert "data-duo-quality-high=" in template
    assert "data-duo-v2-control disabled" in controls
    assert "data-duo-strict-option disabled" in controls
    assert "data-duo-high-option disabled" in controls
    assert "High is reserved for a separately reviewed topology" in controls
    assert 'contractVersion.value = trio ? "3"' in script
    assert "control.disabled = !controlledEnabled" in script
    assert "without controlled multi-character capabilities" in script
    assert 'isolation.value = mode === "duo" && controlledEnabled && capability.strict' in script
    assert "highOption.disabled = true" in script
    assert '["draft", "standard"].includes(variant.duo_quality_mode)' in script
    assert "fixed isolated-refinement topology" in script
    assert "Strict adds isolated refinement passes." in script
    assert "Exact billed time depends on image size, steps, and worker throughput." in script
    assert "any small or large" in controls
    assert "one compatible GPU stays warm" in controls
    assert "LoRAs are shared model-wide" in controls
    assert "must not be treated as character-local controls" in controls


def test_controlled_duo_swap_moves_the_whole_character_contract() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "characterA.subject.value = characterB.subject.value" in script
    assert "characterA.prompt.value = characterB.prompt.value" in script
    assert "characterA.negative.value = characterB.negative.value" in script
    assert "characterA.pose.value = characterB.pose.value" in script
    assert "characterB.subject.value = values.subject" in script
    assert "characterB.prompt.value = values.prompt" in script
    assert "characterB.negative.value = values.negative" in script
    assert "characterB.pose.value = values.pose" in script
    assert 'key: "c"' in script
    assert "Swap complete A / B" not in script


def test_single_duo_toggle_has_one_capability_driven_owner_and_restores_contract() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "initializeCharacterComposition" not in script
    assert "const selectWorkflowForMode = (mode) =>" in script
    assert 'option?.dataset.duoContractV2 === "true"' in script
    assert 'option?.dataset.regionalPrompting === "true"' in script
    assert 'previousMode === "single" && nextMode !== "single"' in script
    assert "restoreMultiContract();" in script
    assert "clearMultiContractForSingle();" in script
    assert "cacheMultiContract();" in script
    assert "builder.dataset[`controlledNegative${key}`] = negative.value" in script
    assert "builder.dataset[`controlledPose${key}`] = pose.value" in script
    assert "builder.dataset.controlledInteraction = interaction.value" in script
    assert "selectWorkflowForMode(nextMode);" in script
    assert 'if (!control.value) return "";' in script


def test_controlled_duo_pose_wildcards_and_batch_overrides_follow_automation_state() -> None:
    new_set = NEW_SET.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")

    for field in (
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
    ):
        assert f'"{field}"' in script
    assert 'data-batch-field="character_{{ character }}_prompt"' in new_set
    assert 'data-batch-field="character_{{ character }}_pose_prompt"' in new_set
    assert 'data-batch-field="character_{{ character }}_negative_prompt"' in new_set
    assert "An enabled blank field explicitly clears" in new_set
    assert "switch Override off to inherit again" in new_set
    assert "Import pose wildcard" in new_set
    assert "controlledCompositionIsActive" in script
    assert "CONTROLLED_BATCH_OVERRIDE_FIELDS" in script
    assert "optionalPrompts[fieldName] = value" in script
    assert "toggle.checked" in script
    assert "insertPromptToken" in script
    assert "data-wildcard-aware" in new_set
    assert '[data-duo-contract-version="2"]' in styles
    assert '[data-duo-contract-version="3"]' in styles


def test_controlled_trio_is_capability_gated_balanced_and_pose_free() -> None:
    new_set = NEW_SET.read_text(encoding="utf-8")
    controls = CONTROLS.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")
    wildcards = WILDCARDS.read_text(encoding="utf-8")

    assert "{% if trio_available %}" in new_set
    assert 'value="trio"' in new_set
    assert "Three characters" in new_set
    assert 'name="subject_3_id"' in new_set
    for preset in ("trio_flexible", "trio_row", "trio_triangle", "trio_depth"):
        assert f'value="{preset}"' in controls
        assert f"{preset}:" in script
        assert f'[data-preset="{preset}"]' in styles
    assert 'option?.dataset.trioContractV1 === "true"' in script
    assert "option.dataset.trioContractV1 === current?.dataset.trioContractV1" in script
    assert "trio && capability.trio" in script
    assert 'contractVersion.value = trio ? "3"' in script
    assert "strictOption.disabled = !controlledEnabled || trio" in script
    assert "Controlled Trio v1 is reviewed for balanced identity-region guidance only." in script
    assert "These are optional identity-region guides, not pose choices." in controls
    assert "data-signed-outputs-per-job-cap" in new_set
    assert "automatically split into signed-request-safe internal chunks" in new_set
    assert "Math.min(requestedPerJob, signedOutputsPerJobCap)" in script
    assert "your batch sizes are unchanged" in script
    assert script.count('interaction: "",') >= 11
    assert "back-to-back, looking in opposite directions" not in script
    assert "A/B/C" in wildcards
    assert "every character uses the same row" in wildcards


def test_switching_from_trio_to_duo_disables_character_c_form_controls() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "const [characterA, characterB, characterC] = characterControls" in script
    assert "const inactiveCharacterCControl = characterC.card instanceof HTMLElement" in script
    assert "characterC.card.contains(control) && !trio" in script
    assert "control.disabled = !controlledEnabled || inactiveCharacterCControl" in script
