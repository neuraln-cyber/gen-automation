import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_asset_cards_offer_lazy_generation_settings_with_json_fallback() -> None:
    template = (
        ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "review_task.html"
    ).read_text(encoding="utf-8")
    bootstrap = (
        ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "release_detail.html"
    ).read_text(encoding="utf-8")

    assert "data-generation-details" in template
    assert "Full prompt &amp; generation settings" in template
    assert "/dashboard/assets/{{" in template
    assert "/generation-details" in template
    assert "Open sanitized settings JSON" in template
    assert "data-generation-details" not in bootstrap
    assert "data-review-bootstrap" in bootstrap


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
    assert "Set presets" in template
    assert "every named batch and prompt" in template
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
    assert '"subject_2_id"' in script
    assert '"composition_mode"' in script
    assert '"character_a_prompt"' in script
    assert '"character_b_prompt"' in script
    assert "secondary_subject_name" in script
    assert "secondary_subject_slug" in script
    assert '["duo", "trio"].includes(compositionSource.mode)' in script
    assert '"Composition: two characters (A / B)"' in script
    assert '"Composition: three characters (A / B / C)"' in script
    assert "Character A identity / appearance" in script
    assert "Character B identity / appearance" in script
    preset_fields = script.split("AUTOMATION_PRESET_FIELDS", maxsplit=1)[1].split(
        "]);", maxsplit=1
    )[0]
    assert '"seed"' not in preset_fields
    assert '"desired_accepted_count"' not in preset_fields
    assert "scopedStorageKey" in script
    assert "draft.submission_id !== submittedDraftId" in script


def test_generation_details_and_reuse_round_trip_the_controlled_trio_contract() -> None:
    script = (ROOT / "src" / "gen_automation" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    sanitizer = script.split("function sanitizedGenerationDetails", maxsplit=1)[1].split(
        "const createNode", maxsplit=1
    )[0]
    for prompt_name in (
        "character_c",
        "character_a_pose",
        "character_b_pose",
        "character_c_pose",
        "character_a_negative",
        "character_b_negative",
        "character_c_negative",
        "interaction",
        "camera",
    ):
        assert f'"{prompt_name}"' in sanitizer
    for composition_field in (
        "contract_version",
        "preset_id",
        "isolation_mode",
        "quality_mode",
    ):
        assert f'"{composition_field}"' in sanitizer

    reuse = script.split("const automationProfileFromDetails", maxsplit=1)[1].split(
        "const reuseImageSettingsButton", maxsplit=1
    )[0]
    for form_field in (
        "character_c_prompt",
        "character_a_pose_prompt",
        "character_b_pose_prompt",
        "character_c_pose_prompt",
        "character_a_negative_prompt",
        "character_b_negative_prompt",
        "character_c_negative_prompt",
        "interaction_prompt",
        "camera_prompt",
        "composition_preset_id",
        "duo_isolation_mode",
        "duo_quality_mode",
    ):
        assert f'"{form_field}"' in reuse
    assert 'details.composition.mode === "trio"' in reuse
    assert '"3"' in reuse
    assert "tertiary_subject_name: details.subjects[2]?.name" in reuse


def test_generation_presets_capture_and_restore_complete_ordered_batch_queues() -> None:
    template = (
        ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "new_set.html"
    ).read_text(encoding="utf-8")
    script = (ROOT / "src" / "gen_automation" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    assert "Save preset" in template
    assert 'aria-atomic="true"' in template
    assert "normalizeAutomationPresetBatchPlan" in script
    assert "collectAutomationPresetBatchPlan" in script
    assert "summarizeAutomationBatchPlan" in script
    assert "JSON.stringify(requested).length > 400_000" in script
    assert "const maximumSeed = 9223372036854775807n" in script
    for field in (
        "name",
        "image_count",
        "prompt",
        "negative_prompt",
        "detailer_prompt",
        "detailer_negative_prompt",
        "seed",
    ):
        assert field in script

    save_handler = script.split('saveButton.addEventListener("click"', maxsplit=1)[1].split(
        'loadButton.addEventListener("click"', maxsplit=1
    )[0]
    assert "const currentBatchPlan = collectAutomationPresetBatchPlan(form)" in save_handler
    assert "batch_plan: batchPlan" in save_handler
    assert "batch_count: batchSummary.batchCount" in save_handler
    assert "image_count: batchSummary.imageCount" in save_handler
    assert "currentBatchPlan || preservedBatchPlan" in save_handler

    load_handler = script.split('loadButton.addEventListener("click"', maxsplit=1)[1].split(
        'deleteButton.addEventListener("click"', maxsplit=1
    )[0]
    assert load_handler.index("applyAutomationProfile") < load_handler.index(
        "gen-automation:replace-batch-plan"
    )
    assert "if (savedBatchPlan)" in load_handler
    assert 'new CustomEvent("gen-automation:refresh-batch-plan")' in load_handler
    assert "Older settings-only preset loaded" in load_handler
    assert "Queue replaced" in load_handler

    assert 'negative_prompt: field(row, "negative_prompt").value' in script
    assert '(fieldName !== "negative_prompt" && value === "")' in script
    assert 'form.addEventListener("gen-automation:replace-batch-plan"' in script
    assert "event.detail.replaced = replaceBatchPlan(event.detail.batch_plan)" in script
    assert 'form.addEventListener("gen-automation:refresh-batch-plan", updateBuilder)' in script
    assert 'if (form.dataset.applyingAutomationProfile === "true") return' in script
    assert "const addBatch = (batch, before = null, deferUpdate = false)" in script
    assert "addBatch(batch, null, true)" in script
    assert 'link.download = "automation-presets.json"' in script
    assert "schema_version: 2" in script


def test_generation_form_uses_one_responsive_progressive_builder() -> None:
    template = (
        ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "new_set.html"
    ).read_text(encoding="utf-8")
    styles = (ROOT / "src" / "gen_automation" / "static" / "dashboard_ux.css").read_text(
        encoding="utf-8"
    )
    script = (ROOT / "src" / "gen_automation" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    assert '<div class="automation-create{% if experiment_mode %}' in template
    assert "<h1>New set</h1>" in template
    assert 'class="automation-jump-nav{% if experiment_mode %}' in template
    assert 'class="step-panel form-disclosure automation-settings-panel"' in template
    assert "data-image-settings-summary" in template
    assert 'class="batch-prompts-grid"' in template
    assert "data-queue-summary" in template
    assert 'name="desired_accepted_count"' not in template
    assert 'id="final-set-target"' not in template
    assert ".automation-create-form .automation-sidebar { display: none; }" in styles
    assert ".automation-create-form .mobile-queue-dock { display: none !important; }" in styles

    assert "initializeImageSettingsSummary" in script
    assert 'form.addEventListener("gen-automation:profile-changed", render)' in script


def test_experiment_lab_has_a_scoped_workspace_gallery_and_exact_settings_reuse() -> None:
    template = (
        ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "new_set.html"
    ).read_text(encoding="utf-8")
    styles = (ROOT / "src" / "gen_automation" / "static" / "dashboard_ux.css").read_text(
        encoding="utf-8"
    )
    script = (ROOT / "src" / "gen_automation" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    for contract in (
        "data-experiment-lab-workspace",
        "data-experiment-lab-primary-prompts",
        "data-experiment-lab-output",
        "data-experiment-lab-active-image",
        "data-experiment-lab-gallery",
        "data-experiment-lab-reuse",
        "data-warm-countdown-value",
    ):
        assert contract in template
    assert ".experiment-lab-form .automation-layout" in styles
    assert ".experiment-lab-prompt-shell" in styles
    assert ".experiment-lab-primary-prompts" in styles
    assert ".experiment-lab-title-field" in styles
    assert ".experiment-lab-prompt-actions .queue-submit" in styles
    assert '[data-composition-mode="single"] .character-composition-grid' in styles
    assert ".experiment-lab-active-media img" in styles
    assert ".experiment-lab-thumbnail.is-selected" in styles
    assert ".experiment-lab-form .automation-sidebar { display: grid; }" in styles

    workspace = script.split("function initializeExperimentLabWorkspace", maxsplit=1)[1].split(
        "function initializeExperimentResults", maxsplit=1
    )[0]
    assert "generation_details_url" in workspace
    assert "sanitizedGenerationDetails" in workspace
    assert "applyGenerationDetailsToExperimentLab" in workspace
    assert "gen-automation:assets-updated" in workspace
    assert "payload.next_cursor" in workspace
    assert "button.dataset.experimentLabThumbnail" in workspace
    assert "innerHTML" not in workspace

    warm = script.split("function initializeExperimentWarmSession", maxsplit=1)[1].split(
        "function initializeExperimentLabWorkspace", maxsplit=1
    )[0]
    assert "payload.expires_at" in warm
    assert "payload.hard_expires_at" in warm
    assert "window.setInterval(renderCountdown, 1000)" in warm

    identity_reset = script.split("const resetQueuedExperimentDraftIdentity", maxsplit=1)[1].split(
        "function restoreAutomationDraft", maxsplit=1
    )[0]
    assert "data-experiment-lab-form" in identity_reset
    assert "experimentReleaseId" in identity_reset
    assert "submittedDraftId" in identity_reset
    assert "draft.submission_id !== submittedDraftId" in identity_reset
    assert "experimentDraftForNextRun(draft)" in identity_reset

    builder = script.split("function initializeAutomationBuilder", maxsplit=1)[1].split(
        "function initializeAutomationDraft", maxsplit=1
    )[0]
    assert 'form.matches("[data-experiment-lab-form]")' in builder
    assert "syncExperimentTopPromptsFromFirstBatch" in builder
    assert "experimentLabMode ? rows.slice(0, 1) : rows" in builder
    assert 'field(first, "prompt").value' in builder
    assert 'field(first, "negative_prompt").value' in builder


def test_live_generation_exposes_failures_and_hides_unready_review_action() -> None:
    template = (
        ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "new_set_status.html"
    ).read_text(encoding="utf-8")
    styles = (ROOT / "src" / "gen_automation" / "static" / "dashboard_ux.css").read_text(
        encoding="utf-8"
    )
    script = (ROOT / "src" / "gen_automation" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    assert "release.jobs.failed }} failed" in template
    assert "unfinished_jobs }} remaining" in template
    assert ".generation-progress-next[hidden] { display: none !important; }" in styles
    assert "const failedJobs = safeCount(payload.jobs.failed)" in script
    assert "jobSummary.push(`${failedJobs} failed`)" in script
    assert "jobSummary.push(`${unfinishedJobs} remaining`)" in script


def test_fresh_generation_queue_automatically_keeps_every_planned_image_eligible() -> None:
    template = (
        ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "new_set.html"
    ).read_text(encoding="utf-8")
    script = (ROOT / "src" / "gen_automation" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    assert "Maximum final set" in template
    assert 'name="desired_accepted_count"' not in template
    assert "maximum images to keep" not in template
    assert "Keep all generated" not in template
    assert "const defaultFinalSetCap" not in script
    assert "targetUsesSmartDefault" not in script
    assert "targetFollowsQueue" not in script
    assert "const finalSetSize = Math.min(totalImages, maximumFinalSetSize)" in script
    assert "targetSummary.textContent = finalSetSize.toLocaleString()" in script


def test_batch_negative_prompt_is_visible_editable_and_tracks_shared_defaults() -> None:
    template = (
        ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "new_set.html"
    ).read_text(encoding="utf-8")
    script = (ROOT / "src" / "gen_automation" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    batch_template = template.split('<template id="batch-row-template">', maxsplit=1)[1]
    assert batch_template.index('data-batch-field="negative_prompt"') < batch_template.index(
        '<details class="batch-overrides">'
    )
    assert 'aria-label="Negative prompt"' in batch_template
    assert 'data-batch-wildcard-target="negative_prompt"' in batch_template
    assert 'event.target.dataset.batchWildcardTarget || "prompt"' in script
    assert 'previousDefaultNegative = defaultNegative ? defaultNegative.value : ""' in script
    assert "negativePrompt.value === previousDefaultNegative" in script


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
