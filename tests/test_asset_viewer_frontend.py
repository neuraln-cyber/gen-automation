from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "src" / "gen_automation" / "static" / "asset_viewer.js"
STYLES = ROOT / "src" / "gen_automation" / "static" / "dashboard_ux.css"
DASHBOARD_SCRIPT = ROOT / "src" / "gen_automation" / "static" / "dashboard.js"


def test_asset_viewer_is_safe_progressive_enhancement() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "initializeAssetViewer" in script
    assert "data-asset-viewer-trigger" in script
    assert "showModal" in script
    assert 'event.key === "ArrowLeft"' in script
    assert 'event.key === "ArrowRight"' in script
    assert 'event.key === "Delete"' in script
    assert 'event.key === "Escape"' in script
    assert 'event.key === "Backspace"' not in script
    assert "isEditableTarget(event.target)" in script
    assert "returnFocus.focus()" in script
    assert "new Image()" in script
    assert "visibleCards()" in script
    assert "Download exact raw master" in script
    assert "assetViewerSelect" in script
    assert 'selection.dispatchEvent(new Event("change", { bubbles: true }))' in script
    assert "assetViewerSettings" in script
    assert "assetViewerFitToggle" in script
    assert 'viewer.dialog.dataset.assetViewerMode = actualSize ? "actual" : "fit"' in script
    assert 'viewer.dialog.classList.toggle("asset-viewer-actual-size", actualSize)' in script
    assert "resetViewerScroll" in script
    assert "viewer.media.scrollTop = 0" in script
    assert "viewer.media.scrollLeft = 0" in script
    assert "asset-viewer-shortcuts" in script
    assert "Navigate" in script
    assert "assetViewerMarkOut" in script
    assert "assetViewerAnatomyReject" in script
    assert "Remove + anatomy label" in script
    assert "data-review-inspection-form" in script
    assert "queueInspection(activeCard)" in script
    assert "flushInspectionQueue(true)" in script
    assert "drainInspectionQueue" in script
    assert "INSPECTION_BATCH_SIZE = 500" in script
    assert "INSPECTION_IDLE_FLUSH_MS = 5000" in script
    assert "assetViewerDefectPicker" in script
    assert "data-defect-code" in script
    assert 'event.key.toLowerCase() === "a"' in script
    assert "form[data-review-decision-form]" in script
    assert 'button[data-decision="reject"]' in script
    assert "submitRejection" in script
    assert "context.form.requestSubmit(context.rejectButton)" in script
    assert "pendingRejection" in script
    assert "rejectionBusy" in script
    assert 'document.addEventListener("gen-automation:review-action-settled"' in script
    assert 'card.dataset.decision === "reject"' in script
    assert "viewer.markOut.textContent = rejectionBusy" in script
    assert "raw master retained" in script
    assert "step(1)" in script
    assert "submitRejection(event.shiftKey, {" in script
    assert 'viewer.markOut.addEventListener("click", () => submitRejection(false, {' in script
    assert 'viewer.anatomyReject.addEventListener("click", () => submitRejection(true))' in script
    assert "assetViewerSaveExclusions" not in script
    assert "markedForExclusion" not in script
    assert "review-exclusions:v1" not in script
    assert "restoreMarkedExclusions" not in script
    assert "viewer-marked-for-exclusion" not in script
    assert "bulkForm.requestSubmit(bulkReject)" not in script
    assert "assetViewerCopyClean" in script
    assert "assetViewerDownloadClean" in script
    assert "Copy clean image" in script
    assert "Download clean PNG" in script
    assert "Download exact raw master" in script
    assert "Select for bulk action" in script
    assert 'viewer.media.addEventListener("touchstart"' in script
    assert "cleanPngFor" in script
    assert "new URL(source, window.location.href)" in script
    assert "fetch(sourceUrl.href" in script
    assert 'cache: "no-store"' in script
    assert 'sourceUrl.origin === window.location.origin ? "same-origin" : "omit"' in script
    assert "window.createImageBitmap(sourceBlob)" in script
    assert 'document.createElement("canvas")' in script
    assert "context.drawImage(bitmap, 0, 0)" in script
    assert "canvas.toBlob((blob) => {" in script
    assert '}, "image/png")' in script
    assert "window.ClipboardItem" in script
    assert 'new window.ClipboardItem({ "image/png": cleanPng })' in script
    assert "navigator.clipboard.write([item])" in script
    assert "URL.createObjectURL(blob)" in script
    assert "URL.revokeObjectURL(objectUrl)" in script
    assert "embedded generation metadata was not included" in script
    assert "exact raw master remains unchanged" in script
    assert 'activeCard.querySelector("[data-generation-details]")' in script
    assert "Rank score" in script
    assert "ensureTrigger" in script
    assert 'createElement("button", "asset-viewer-open-button", "Full screen")' in script
    assert 'button.type = "button"' in script
    assert "const boundCards = new WeakSet()" in script
    assert 'document.addEventListener("gen-automation:assets-updated"' in script
    assert 'root.querySelectorAll("[data-asset-card], .asset-card")' in script
    assert 'card.closest("[data-asset-grid]")' in script
    assert "boundCards.has(card)" in script
    assert "assetCards().find" in script
    assert "card.dataset.assetLabel" in script
    assert 'if (!document.querySelector("[data-asset-grid]")) return;' in script
    assert "${details.rank}${scoreAnnouncement}" in script
    assert "const scrollTarget = summary || settingsPanel" in script
    assert 'block: "center"' in script
    assert "focus({ preventScroll: true })" in script
    assert "textContent" in script
    assert "innerHTML" not in script
    assert "document.write" not in script
    assert "eval(" not in script


def test_asset_viewer_waits_for_persisted_rejection_before_advancing() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    submission = script.split("const submitRejection", 1)[1].split("const openViewer", 1)[0]
    settlement = script.split(
        'document.addEventListener("gen-automation:review-action-settled"',
        1,
    )[1].split('viewer.close.addEventListener("click"', 1)[0]

    pending_index = submission.index("pendingRejection =")
    busy_index = submission.index("rejectionBusy = true")
    request_index = submission.index("context.form.requestSubmit(context.rejectButton)")
    request_return_index = submission.index("return true", request_index)

    assert "nextAssetId" in submission
    assert pending_index < busy_index < request_index
    assert "renderCard(" not in submission[request_index:request_return_index]
    assert "step(" not in submission[request_index:request_return_index]

    failure = settlement.split("if (!detail.success)", 1)[1].split(
        "announcement = settled.removingAnatomyLabel",
        1,
    )[0]
    assert settlement.index("pendingRejection = null") < settlement.index("if (!detail.success)")
    assert settlement.index("rejectionBusy = false") < settlement.index("if (!detail.success)")
    assert "if (!detail.success)" in settlement
    assert "updateRejectionControls()" in failure
    assert "This image is still in the final set; try again." in failure
    assert "return;" in failure
    assert "renderCard(" not in failure
    assert "settled.nextAssetId" in settlement
    assert "renderCard(nextCard)" in settlement
    assert settlement.index("if (!detail.success)") < settlement.index("renderCard(nextCard)")


def test_asset_viewer_delete_shortcuts_distinguish_plain_and_anatomy_rejection() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    keyboard = script.split('viewer.dialog.addEventListener("keydown"', 1)[1].split(
        'viewer.dialog.addEventListener("close"',
        1,
    )[0]

    assert 'event.key === "Delete"' in keyboard
    assert "event.preventDefault()" in keyboard
    assert "submitRejection(event.shiftKey, {" in keyboard
    assert "removeAnatomyLabel: !event.shiftKey && Boolean(context.anatomyLabeled)" in keyboard
    assert "Shift+Del Remove + anatomy" in script
    assert (
        "if (context.anatomyToggle) context.anatomyToggle.checked = withAnatomyTraining" in script
    )


def test_asset_viewer_rejects_immediately_through_the_active_cards_own_form() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    context = script.split("const rejectionContext", 1)[1].split(
        "const updateRejectionControls",
        1,
    )[0]
    submission = script.split("const submitRejection", 1)[1].split("const openViewer", 1)[0]

    assert 'card.querySelector("form[data-review-decision-form]")' in context
    assert "form?.querySelector('button[data-decision=\"reject\"]')" in context
    assert "context.form.requestSubmit(context.rejectButton)" in submission
    assert "bulkForm" not in submission
    assert "bulkReject" not in submission


def test_asset_viewer_has_no_staged_exclusion_queue_or_save_step() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    template = (
        ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "review_task.html"
    ).read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")

    assert "sessionStorage" not in script
    for removed_contract in (
        "assetViewerSaveExclusions",
        "markedForExclusion",
        "review-exclusions:v1",
        "restoreMarkedExclusions",
        "viewer-marked-for-exclusion",
        "Save exclusions",
    ):
        assert removed_contract not in script
        assert removed_contract not in template
        assert removed_contract not in styles


def test_asset_density_is_bounded_persisted_and_accessible() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert 'Object.freeze(["comfortable", "compact", "large"])' in script
    assert "window.localStorage.getItem(STORAGE_KEY)" in script
    assert "window.localStorage.setItem(STORAGE_KEY, value)" in script
    assert 'controls.setAttribute("role", "group")' in script
    assert 'controls.setAttribute("aria-label", "Image card size")' in script
    assert 'button.setAttribute("aria-pressed", "false")' in script


def test_asset_grid_uses_intrinsic_image_ratio_and_viewer_is_responsive() -> None:
    styles = STYLES.read_text(encoding="utf-8")

    assert "[data-asset-grid] .asset-preview" in styles
    assert "height: auto" in styles
    assert "aspect-ratio: auto" in styles
    assert "max-height: none" in styles
    assert "aspect-ratio: 4 / 5" not in styles
    assert '[data-asset-density="compact"]' in styles
    assert '[data-asset-density="large"]' in styles
    assert ".asset-viewer::backdrop" in styles
    assert ".asset-viewer-open-button" in styles
    assert ".asset-media > .asset-viewer-open-button" in styles
    assert "height: 100dvh" in styles
    assert '.asset-viewer[data-asset-viewer-mode="actual"] .asset-viewer-media' in styles
    assert ".asset-viewer-actual-size .asset-viewer-image" in styles
    assert "overflow: auto" in styles
    assert ".asset-viewer-fit-toggle" in styles
    assert ".asset-viewer-mark-out" in styles
    assert ".asset-viewer-anatomy-reject" in styles
    assert ".asset-viewer-copy-clean" in styles
    assert ".asset-viewer-download-clean" in styles
    assert ".asset-viewer-more-body" in styles
    assert ".asset-viewer-previous" in styles
    assert ".asset-viewer-next" in styles
    assert ".asset-card.decision-reject" in styles
    assert 'content: "Removed"' in styles
    assert ".asset-viewer-save-exclusions" not in styles
    assert ".viewer-marked-for-exclusion" not in styles
    assert "env(safe-area-inset-bottom)" in styles
    assert "@media (max-width: 680px)" in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles
    assert ".live-generation-assets" in styles
    assert ".live-generation-assets-empty:not([hidden]) ~ .asset-grid-density" in styles
    assert ".live-generation-assets-grid:empty" in styles


def test_live_generation_surfaces_latest_without_reordering_the_canonical_grid() -> None:
    template = (
        ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "new_set_status.html"
    ).read_text(encoding="utf-8")
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")

    latest_index = template.index("data-live-assets-latest")
    grid_index = template.index("data-live-assets-grid\n")
    assert latest_index < grid_index
    assert "data-live-assets-latest-image" in template
    assert template.count("data-live-assets-latest-open") == 2
    assert "The complete grid below stays in the planned queue" in template
    assert "Planned batch and image order is preserved here." in template

    live_assets = script.split("function initializeLiveGeneratedAssets()", 1)[1].split(
        "function initializeGenerationProgress()",
        1,
    )[0]
    add_assets = live_assets.split("const addAssets =", 1)[1].split("const schedule =", 1)[0]
    assert "const renderLatestAsset" in live_assets
    assert "let latestAssetId" in live_assets
    assert "latestAdded = asset" in add_assets
    assert add_assets.index("reorderCards();") < add_assets.index("renderLatestAsset(latestAdded);")
    assert "grid.insertBefore(asset.card" in live_assets
    assert "grid.prepend" not in live_assets
    assert "assets.get(latestAssetId)?.card" in live_assets
    assert 'card.querySelector("[data-asset-viewer-trigger], .asset-preview")' in live_assets

    assert ".live-generation-latest" in styles
    assert "max-height: min(62dvh, 44rem)" in styles
    assert ".live-generation-latest-image { max-height: 56dvh; }" in styles
    assert "grid-template-columns: minmax(0, 1fr)" in styles


def test_review_filter_hides_empty_ai_excluded_separator() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")

    assert "updateExcludedHeadings" in script
    assert 'document.querySelectorAll(".ai-excluded-heading")' in script
    assert "grid.querySelectorAll('.asset-card[data-ai-excluded=\"true\"]')" in script
    assert "heading.hidden = !hasVisibleExcluded" in script


def test_review_ui_treats_the_configured_size_as_a_maximum_goal() -> None:
    template = (
        ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "review_task.html"
    ).read_text(encoding="utf-8")
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")

    assert "All images start in the final set" in template
    assert "Keep what works, remove what does not" in template
    assert "Finish set with {{ summary.accepted_count }}" in template
    assert "Optional slots left" in template
    assert "Over goal" in template
    assert "Restore at least one image before finishing this set" in template
    assert "Remove {{ accepted_over_goal }} image" in template
    assert "const netNewAccepts" in script
    assert "const exceedsReviewGoal" in script
    assert (
        "acceptButton.disabled = selected === 0 || hasSevereSelection || exceedsReviewGoal"
        in script
    )
    assert "over its maximum; remove kept images before restoring more" in script
    assert "Final-set target reached" in template


def test_review_template_integrates_anatomy_rejection_and_legacy_keep_all() -> None:
    template = (
        ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "review_task.html"
    ).read_text(encoding="utf-8")
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")

    assert "data-accept-undecided" in template
    assert "Keep all {{ summary.undecided_count }} undecided" in template
    assert "data-anatomy-training-control" in template
    assert "data-anatomy-training-toggle" in template
    assert "data-anatomy-training-issue" in template
    assert "Teach anatomy from this removal" in template
    assert "becomes calibration data" in template
    assert "Plain removal only removes an image" in template
    assert "from the final set" in template
    assert 'document.querySelector("[data-accept-undecided]")' in script
    assert 'bulkReasonInput.value = "sorting_default_accept"' in script
    assert "form.requestSubmit(acceptButton)" in script
    assert "anatomyRequested" in script
    assert "reasonInput.value = anatomyIssue instanceof HTMLSelectElement" in script
    assert ".anatomy-reject-option" in styles


def test_fullscreen_inspections_are_batched_retried_and_flushed_without_rendering() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    template = (
        ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "review_task.html"
    ).read_text(encoding="utf-8")

    queue = script.split("const queueInspection", 1)[1].split(
        "const flushInspectionQueue",
        1,
    )[0]
    flush = script.split("const flushInspectionQueue", 1)[1].split(
        "const drainInspectionQueue",
        1,
    )[0]
    step = script.split("const step", 1)[1].split("const submitRejection", 1)[0]
    close = script.split("const closeViewer", 1)[1].split("const renderCard", 1)[0]

    assert 'action="{{ inspection_endpoint }}"' in template
    assert 'value="{{ inspection_idempotency_key }}"' in template
    assert "data-inspected=\"{{ 'true' if item.inspected else 'false' }}\"" in template
    assert "data-inspected-chip" in template
    assert "pendingInspectionIds.add(assetId)" in queue
    assert "scheduleInspectionFlush()" in queue
    assert 'body.append("asset_id", assetId)' in flush
    assert 'Accept: "application/json"' in flush
    assert "failedInspectionBatch = batch" in flush
    assert "idempotencyKey: config.keyPrefix" in flush
    assert "let batch = failedInspectionBatch" in flush
    assert "workspace.replaceWith" not in flush
    assert "renderCard" not in flush
    assert (
        "if (activeCard) queueInspection(activeCard)"
        in script.split(
            "const drainInspectionQueue",
            1,
        )[1].split("const updateCleanControls", 1)[0]
    )
    assert step.index("queueInspection(activeCard)") < step.index("renderCard(")
    assert "queueInspection(activeCard)" in close
    assert "flushInspectionQueue(true)" in close
    assert "restoreInspectionState()" in script
    assert ".decision-chip.inspected" in STYLES.read_text(encoding="utf-8")


def test_fullscreen_inspection_requires_successfully_loaded_exact_asset_source() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    queue = script.split("const queueInspection", 1)[1].split(
        "const flushInspectionQueue",
        1,
    )[0]
    render = script.split("const renderCard", 1)[1].split("const step", 1)[0]
    image_events = script.split('viewer.image.addEventListener("error"', 1)[1].split(
        "let touchStart",
        1,
    )[0]

    assert "const successfullyViewedSources = new Map()" in script
    assert "successfullyViewedSources.get(assetId) !== source" in queue
    assert "requestedSource !== renderedSource" in script
    assert "viewer.image.naturalWidth <= 0" in script
    assert "normalizedSource(sourceFor(activeCard)) !== requestedSource" in script
    assert "viewer.image.dataset.inspectionAssetId" in render
    assert "viewer.image.dataset.inspectionSource" in render
    assert "viewer.image.complete && viewer.image.naturalWidth > 0" in render
    assert "markViewerImageLoaded()" in render
    assert "clearFailedViewerImage()" in image_events
    assert "markViewerImageLoaded()" in image_events


def test_fullscreen_defect_picker_is_optional_exact_and_per_image() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")

    sync = script.split("const syncDefectPicker", 1)[1].split(
        "const updateRejectionControls",
        1,
    )[0]
    submission = script.split("const submitRejection", 1)[1].split("const openViewer", 1)[0]

    assert "context.anatomyIssue.options" in sync
    assert 'context.anatomyIssue.value || "anatomy"' in sync
    assert "chip.dataset.defectCode = option.value" in sync
    assert 'chip.setAttribute("role", "radio")' in sync
    assert "selectedOptions[0]" in script
    assert "Generic is enough" in script
    assert "until the set is finished" in script
    assert "previousAnatomyChecked" in submission
    assert "context.anatomyToggle.checked = withAnatomyTraining" in submission
    assert ".asset-viewer-defect-picker" in styles
    assert ".asset-viewer-defect-chip" in styles
    assert "data-saved-anatomy-issue" in (
        ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "review_task.html"
    ).read_text(encoding="utf-8")
