from pathlib import Path

ROOT = Path(__file__).parents[1]
DASHBOARD_SCRIPT = ROOT / "src/gen_automation/static/dashboard.js"
ASSET_VIEWER_SCRIPT = ROOT / "src/gen_automation/static/asset_viewer.js"
REVIEW_TEMPLATE = ROOT / "src/gen_automation/templates/dashboard/review_task.html"
STYLES = ROOT / "src/gen_automation/static/dashboard_ux.css"


def test_review_mutations_are_progressively_enhanced_without_page_reload() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
    handler = script.split("function initializeReviewActions()", 1)[1].split("const isRecord", 1)[0]

    assert "REVIEW_ACTION_FORM_SELECTOR" in script
    assert "form[data-review-decision-form]" in script
    assert "form[data-bulk-action-form]" in script
    assert "form[data-x-selection-form]" in script
    assert "form[data-anatomy-feedback-form]" in script
    assert "event.preventDefault()" in handler
    assert "await fetch(form.action" in handler
    assert 'redirect: "follow"' in handler
    assert "new DOMParser().parseFromString" in handler
    assert 'parsed.querySelector("[data-review-workspace]")' in handler
    assert "workspace.replaceWith(nextWorkspace)" in handler
    assert "window.location.reload()" not in handler
    assert "HTMLFormElement.prototype.submit" not in handler


def test_review_refresh_restores_scroll_filters_sort_selection_and_focus() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")

    assert "captureReviewViewState" in script
    assert "reviewScrollAnchor" in script
    assert "restoreReviewViewState" in script
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


def test_asset_viewer_rebinds_review_controls_after_in_place_refresh() -> None:
    script = ASSET_VIEWER_SCRIPT.read_text(encoding="utf-8")

    assert "let bulkForm = null" in script
    assert "let bulkReject = null" in script
    assert "refreshReviewControls" in script
    assert 'document.addEventListener("gen-automation:assets-updated"' in script
    assert "const activeAssetId = activeCard?.dataset.assetId" in script
    assert "if (viewer.dialog.open && activeAssetId)" in script
    assert "renderCard(refreshedCard)" in script


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
