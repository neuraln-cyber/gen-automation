from pathlib import Path

ROOT = Path(__file__).parents[1]
DASHBOARD_SCRIPT = ROOT / "src/gen_automation/static/dashboard.js"
ASSET_VIEWER_SCRIPT = ROOT / "src/gen_automation/static/asset_viewer.js"
REVIEW_TEMPLATE = ROOT / "src/gen_automation/templates/dashboard/review_task.html"
RELEASE_TEMPLATE = ROOT / "src/gen_automation/templates/dashboard/release_detail.html"
STYLES = ROOT / "src/gen_automation/static/dashboard_ux.css"


def test_review_mutations_are_progressively_enhanced_without_page_reload() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
    handler = script.split("function initializeReviewActions()", 1)[1].split("const isRecord", 1)[0]

    assert "REVIEW_ACTION_FORM_SELECTOR" in script
    assert "form[data-review-decision-form]" in script
    assert "form[data-bulk-action-form]" in script
    assert "form[data-x-selection-form]" in script
    assert "event.preventDefault()" in handler
    assert "await fetch(form.action" in handler
    assert 'redirect: "follow"' in handler
    assert "new DOMParser().parseFromString" in handler
    assert 'parsed.querySelector("[data-review-workspace]")' in handler
    assert "preserveReviewMedia(workspace, nextWorkspace)" in handler
    assert "workspace.replaceWith(nextWorkspace)" in handler
    assert "window.location.reload()" not in handler
    assert "HTMLFormElement.prototype.submit" not in handler


def test_release_auto_bootstraps_the_single_review_workspace() -> None:
    bootstrap = RELEASE_TEMPLATE.read_text(encoding="utf-8")
    workspace = REVIEW_TEMPLATE.read_text(encoding="utf-8")
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
    initializer = script.split("function initializeReviewBootstrap()", 1)[1].split(
        "const experimentFormPresent",
        1,
    )[0]

    assert "data-review-bootstrap" in bootstrap
    assert 'action="/dashboard/releases/{{ release_id }}/review-tasks"' in bootstrap
    assert "Preparing your review workspace" in bootstrap
    assert "Start ranked review" not in bootstrap
    assert "Open ranked review" not in bootstrap
    assert "Continue review" not in bootstrap

    assert "data-review-workspace" in workspace
    assert "data-open-review-viewer" in workspace
    assert "everything starts kept" in workspace
    assert "data-anatomy-feedback-form" not in workspace
    assert 'data-decision="hold"' not in workspace
    assert "Hold selected" not in workspace

    assert 'document.querySelector("form[data-review-bootstrap]")' in initializer
    assert "window.requestAnimationFrame(submit)" in initializer
    assert "form.requestSubmit()" in initializer
    assert 'form.dataset.reviewBootstrapSubmitted = "true"' in initializer


def test_review_refresh_restores_scroll_filters_sort_selection_and_focus() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")

    assert "captureReviewViewState" in script
    assert "reviewScrollAnchor" in script
    assert "restoreReviewViewState" in script
    assert "const preserveReviewMedia" in script
    assert 'card.querySelector(".asset-preview")' in script
    assert "nextImage.replaceWith(currentImage)" in script
    assert "window.requestAnimationFrame(restoreScroll)" in script
    assert "window.requestAnimationFrame(() => {\n      restoreScroll();" not in script
    assert 'document.querySelector("[data-review-filter].active")' in script
    assert 'document.querySelector("[data-asset-sort].active")' in script
    assert "state.selectedAssetIds.forEach" in script
    assert "anchor.getBoundingClientRect().top - state.anchor.top" in script
    assert "window.scrollBy(0, delta)" in script
    assert "focus({ preventScroll: true })" in script
    assert 'new CustomEvent("gen-automation:assets-updated"' in script
    assert 'new CustomEvent("gen-automation:review-updated"' in script


def test_review_page_has_replaceable_workspace_and_live_action_status() -> None:
    template = REVIEW_TEMPLATE.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")

    assert "data-review-workspace" in template
    assert "data-review-action-status" in template
    assert 'aria-live="polite"' in template
    assert ".review-action-status" in styles
    assert '[data-review-workspace][aria-busy="true"]' in styles


def test_review_completion_hands_off_inspections_with_a_bounded_retryable_request() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
    handler = script.split("function initializeReviewCompletionInspectionFlush()", 1)[1].split(
        "function initializeReviewActions()",
        1,
    )[0]

    assert 'form.matches("[data-review-complete-form]")' in handler
    assert "event.preventDefault()" in handler
    assert "captureCompletionInspectionIds(form)" in handler
    assert "REVIEW_INSPECTION_FLUSH_TIMEOUT_MS" not in script
    assert '"gen-automation:inspection-flush-progress"' not in handler
    assert 'submitter.textContent = "Finishing set..."' in handler
    assert "reviewActionStatusTimers" in script
    assert "false,\n          0," in handler
    assert 'form.dataset.completionSubmitting = "true"' in handler
    assert "REVIEW_COMPLETION_TIMEOUT_MS = 25000" in script
    assert "await fetch(action, options)" in handler
    assert 'redirect: "follow"' in handler
    assert "controller.abort()" in handler
    assert 'error.name === "AbortError"' in handler
    assert "Nothing was lost; click Finish again." in handler
    assert "window.location.assign(destination.href)" in handler
    assert "delete form.dataset.completionSubmitting" in handler
    assert "form.requestSubmit" not in handler
    assert "inspectionFlushPending" not in handler
    request = script.split("const attachCompletionInspectionIds", 1)[1].split(
        "function initializeReviewCompletionInspectionFlush()",
        1,
    )[0]
    assert 'field.name = "inspected_asset_id"' in request
    assert "field.dataset.completionInspectionId" in request
    assert "attachCompletionInspectionIds(form, assetIds)" in request
    assert '"gen-automation:inspection-completion-handoff"' in request


def test_asset_viewer_rebinds_review_controls_after_in_place_refresh() -> None:
    script = ASSET_VIEWER_SCRIPT.read_text(encoding="utf-8")
    refresh_listener = script.split(
        'document.addEventListener("gen-automation:assets-updated"',
        1,
    )[1].split(
        'document.addEventListener("gen-automation:review-action-settled"',
        1,
    )[0]

    assert "const boundCards = new WeakSet()" in script
    assert "bindCards(root || document)" in refresh_listener
    assert "const activeAssetId = activeCard?.dataset.assetId" in refresh_listener
    assert "if (viewer.dialog.open && activeAssetId)" in refresh_listener
    assert "assetCards().find" in refresh_listener
    assert "candidate.dataset.assetId === activeAssetId" in refresh_listener
    assert "renderCard(refreshedCard)" in refresh_listener
    assert refresh_listener.index("bindCards(root || document)") < refresh_listener.index(
        "renderCard(refreshedCard)"
    )


def test_review_refresh_notifies_the_viewer_after_replacing_the_workspace() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
    handler = script.split("function initializeReviewActions()", 1)[1].split("const isRecord", 1)[0]
    restore = script.split("const restoreReviewViewState", 1)[1].split(
        "const showReviewActionStatus",
        1,
    )[0]

    replace_index = handler.index("workspace.replaceWith(nextWorkspace)")
    restore_call_index = handler.index("restoreReviewViewState(nextWorkspace, viewState)")
    success_index = handler.index("actionSucceeded = true")

    assert 'new CustomEvent("gen-automation:assets-updated"' in restore
    assert replace_index < restore_call_index < success_index


def test_server_round_trip_forms_preserve_scroll_application_wide() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")

    assert "SAME_PAGE_SCROLL_STORAGE_KEY" in script
    assert "persistSamePageScroll" in script
    assert "initializeSamePageScrollPreservation" in script
    assert 'document.addEventListener("submit"' in script
    assert "window.sessionStorage.setItem(SAME_PAGE_SCROLL_STORAGE_KEY" in script
    assert "window.sessionStorage.removeItem(SAME_PAGE_SCROLL_STORAGE_KEY)" in script
    assert "window.scrollTo(saved.x, saved.y)" in script
    assert "persistSamePageScroll();\n        window.location.reload();" in script
