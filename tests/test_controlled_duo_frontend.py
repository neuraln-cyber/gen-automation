from pathlib import Path

ROOT = Path(__file__).parents[1]
NEW_SET = ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "new_set.html"
EXPERIMENT = ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "experiment_new.html"
CONTROLS = (
    ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "_controlled_duo_controls.html"
)
SCRIPT = ROOT / "src" / "gen_automation" / "static" / "dashboard.js"
STYLES = ROOT / "src" / "gen_automation" / "static" / "dashboard_ux.css"


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
    for field in ("character_a_negative_prompt", "character_b_negative_prompt"):
        assert f'name="{field}"' in new_set
        assert f'name="{field}"' in experiment


def test_controlled_duo_builder_has_six_composition_presets_and_mask_preflight() -> None:
    controls = CONTROLS.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")

    for preset in (
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
    assert "Two non-overlapping character regions" in controls
    assert "Pair preflight" in controls
    assert "Possible prompt bleed" in script
    assert "system-owned exact-two composition" in script
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
    assert 'contractVersion.value = v2Enabled ? "2" : "1"' in script
    assert "control.disabled = !v2Enabled" in script
    assert "Choose a standard single-character workflow without duo capabilities." in script
    assert 'isolation.value = v2Enabled && capability.strict ? "strict" : "balanced"' in script
    assert "highOption.disabled = true" in script
    assert '["draft", "standard"].includes(variant.duo_quality_mode)' in script
    assert "fixed isolated-refinement topology" in script
    assert "Strict adds isolated refinement passes." in script
    assert "Exact billed time depends on image size, steps, and worker throughput." in script
    assert "paired seeds and 1&ndash;2 images" in controls
    assert "LoRAs are shared model-wide" in controls
    assert "must not be treated as character-local controls" in controls


def test_controlled_duo_swap_moves_the_whole_character_contract() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "firstSubject.value = secondSubject.value" in script
    assert "firstPrompt.value = secondPrompt.value" in script
    assert "firstNegative.value = secondNegative.value" in script
    assert "secondSubject.value = subjectValue" in script
    assert "secondPrompt.value = positiveValue" in script
    assert "secondNegative.value = negativeValue" in script
    assert "Swap complete A / B" not in script


def test_single_duo_toggle_has_one_capability_driven_owner_and_restores_contract() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "initializeCharacterComposition" not in script
    assert "const selectWorkflowForMode = (mode) =>" in script
    assert 'option?.dataset.duoContractV2 === "true"' in script
    assert 'option?.dataset.regionalPrompting === "true"' in script
    assert 'if (nextMode === "duo") restoreDuoContract();' in script
    assert "else clearDuoContractForSingle();" in script
    assert "cacheDuoContract();" in script
    assert "builder.dataset.duoFirstNegative = firstNegative.value" in script
    assert "builder.dataset.duoInteraction = interaction.value" in script
    assert "selectWorkflowForMode(nextMode);" in script
    assert 'if (!control.value) return "";' in script
