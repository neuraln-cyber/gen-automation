import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_asset_cards_offer_lazy_generation_settings_with_json_fallback() -> None:
    for template_name in ("release_detail.html", "review_task.html"):
        template = (
            ROOT / "src" / "gen_automation" / "templates" / "dashboard" / template_name
        ).read_text(encoding="utf-8")
        assert "data-generation-details" in template
        assert "Full prompt &amp; generation settings" in template
        assert "/dashboard/assets/{{" in template
        assert "/generation-details" in template
        assert "Open sanitized settings JSON" in template


def test_generation_settings_renderer_keeps_server_values_out_of_html_sinks() -> None:
    script = (ROOT / "src" / "gen_automation" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    assert "initializeGenerationDetails" in script
    assert 'cache: "no-store"' in script
    assert "sanitizedGenerationDetails" in script
    assert "textContent" in script
    assert "innerHTML" not in script
    assert "document.write" not in script
    assert "eval(" not in script


def test_generation_details_offer_forge_copy_reuse_and_clean_export_context() -> None:
    script = (ROOT / "src" / "gen_automation" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    assert "forgeStyleImageInfo" in script
    assert "Copy Forge-style info" in script
    assert "Negative prompt:" in script
    assert "Schedule type:" in script
    assert "ADetailer model: face_yolov8n.pt" in script
    assert "Lora hashes:" in script
    assert "displayValue(lora.sha256).slice(0, 12)" in script
    assert "Use in new automation" in script
    assert "window.sessionStorage.setItem(" in script
    assert 'window.location.assign("/dashboard/new-set")' in script
    assert "batch_prompt: preferredPromptText(details.prompts.positive)" in script
    assert "PENDING_IMAGE_PROFILE_KEY" in script
    assert "Download clean image" not in script
    assert "without embedded prompt or workflow metadata" in script


def test_generation_form_has_named_device_local_settings_presets() -> None:
    template = (
        ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "new_set.html"
    ).read_text(encoding="utf-8")
    script = (ROOT / "src" / "gen_automation" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    assert "data-automation-presets" in template
    assert "Saved on this device" in template
    assert "data-automation-preset-save" in template
    assert "data-automation-preset-load" in template
    assert "data-automation-preset-delete" in template
    assert "AUTOMATION_PRESET_STORAGE_KEY" in script
    assert "collectAutomationProfile" in script
    assert "applyAutomationProfile" in script
    assert "initializeAutomationPresets" in script
    assert "initializeAutomationLayout" not in script
    identity = template.index("automation-identity-panel")
    profile = template.index("automation-profile-panel")
    settings = template.index("automation-settings-panel")
    batches = template.index("automation-batches-panel")
    assert identity < profile < settings < batches
    assert "consumePendingImageProfile" in script
    assert "loaded with changes needed" in script
    assert "is not currently approved" in script
    assert "is not currently available" in script
    assert "firstInvalidPresetControl" in script
    preset_fields = script.split("AUTOMATION_PRESET_FIELDS", maxsplit=1)[1].split(
        "]);", maxsplit=1
    )[0]
    assert '"seed"' not in preset_fields
    assert '"desired_accepted_count"' not in preset_fields
    assert "scopedStorageKey" in script
    assert "draft.submission_id !== submittedDraftId" in script


def test_generation_prompts_are_keyboard_scrollable_and_contextually_labeled() -> None:
    script = (ROOT / "src" / "gen_automation" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    assert "const promptTextBlock" in script
    assert "block.tabIndex = 0" in script
    assert 'block.setAttribute("role", "region")' in script
    assert 'block.setAttribute("aria-label", label)' in script
    assert "`Copy ${label.toLowerCase()}`" in script
    assert "`Copy ${label.toLowerCase()} template`" in script

    styles = (ROOT / "src" / "gen_automation" / "static" / "dashboard_ux.css").read_text(
        encoding="utf-8"
    )
    assert ".generation-prompt-text:focus-visible" in styles


def test_generation_settings_distinguish_inactive_hires_values_from_an_upscale_pass() -> None:
    script = (ROOT / "src" / "gen_automation" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    assert "const upscalerEnabled = details.hires.enabled === true" in script
    assert '"Full-image upscaler"' in script
    assert '"Off - no upscale node in workflow"' in script
    assert "if (upscalerEnabled)" in script


def test_generation_form_explains_and_hides_inactive_hires_controls() -> None:
    template = (
        ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "new_set.html"
    ).read_text(encoding="utf-8")
    script = (ROOT / "src" / "gen_automation" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    assert "data-upscaler-enabled" in template
    assert "data-workflow-refinement-status" in template
    assert template.count("data-hires-setting") == 3
    assert "data-upscaler-toggle" in template
    assert 'role="switch"' in template
    assert "initializeWorkflowRefinement" in script
    assert "this workflow has no upscale node" in script
    assert "option.dataset.upscalerEnabled === desired" in script
    assert 'workflow.dispatchEvent(new Event("change", { bubbles: true }))' in script
    assert "no upscale node or second sampler pass" in script
    assert "normalizeHiddenHiresValues" in script
    assert "Face maximum size must be at least the face guide size." in script


def test_generation_form_offers_an_unnamed_attached_a1111_sample_preset() -> None:
    template = (
        ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "new_set.html"
    ).read_text(encoding="utf-8")
    script = (ROOT / "src" / "gen_automation" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    control = re.search(r"<button\b[^>]*data-detailer-preset-control[^>]*>", template)
    assert control is not None
    assert "name=" not in control.group(0)
    assert "Apply attached A1111 sample preset" in template
    assert "Matches the attached A1111 sample metadata" in template
    assert "crop factor approximates ADetailer padding semantics" in template
    assert "does not apply full-image upscaling" in template
    assert 'detailer_prompt: "sexy, expressive, "' in script
    assert 'detailer_negative_prompt: "closed eyes, "' in script
    assert 'detailer_guide_size: "768"' in script
    assert 'detailer_max_size: "1536"' in script
    assert 'detailer_denoise: "0.4"' in script
    assert 'detailer_bbox_threshold: "0.3"' in script
    assert 'detailer_bbox_dilation: "4"' in script
    assert 'detailer_bbox_crop_factor: "1.5"' in script
    assert 'detailer_feather: "4"' in script
    assert "detailerPresetControl.disabled = !detailerEnabled" in script
    assert 'field.addEventListener("input", render)' in script
    assert '"Attached A1111 sample" : "Custom"' in script
