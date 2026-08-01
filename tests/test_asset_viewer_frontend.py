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
    assert "assetViewerSaveExclusions" in script
    assert 'document.querySelector("#bulk-action-form, [data-bulk-action-form]")' in script
    assert 'button[name="action"][value="reject"]' in script
    assert "selection.form !== bulkForm" in script
    assert "markedForExclusion" in script
    assert "review-exclusions:v1" in script
    assert "restoreMarkedExclusions" in script
    assert "viewer-marked-for-exclusion" in script
    assert 'card.dataset.decision === "reject"' in script
    assert 'viewer.markOut.textContent = "Excluded"' in script
    assert "raw master retained" in script
    assert "raw masters retained" in script
    assert "step(1)" in script
    assert "selection.disabled = !marked" in script
    assert "selection.checked = marked" in script
    assert "restoreSelections" in script
    assert "bulkForm.requestSubmit(bulkReject)" in script
    assert "assetViewerCopyClean" in script
    assert "assetViewerDownloadClean" in script
    assert "Copy clean image" in script
    assert "Download clean PNG" in script
    assert "Download exact raw master" in script
    assert "Select for bulk action" in script
    assert 'viewer.media.addEventListener("touchstart"' in script
    assert "cleanPngFor" in script
    assert "fetch(source" in script
    assert 'cache: "no-store"' in script
    assert 'credentials: "omit"' in script
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
    assert "${details.rank}${scoreAnnouncement}" in script
    assert "const scrollTarget = summary || settingsPanel" in script
    assert 'block: "center"' in script
    assert "focus({ preventScroll: true })" in script
    assert "textContent" in script
    assert "innerHTML" not in script
    assert "document.write" not in script
    assert "eval(" not in script


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
    assert ".asset-viewer-save-exclusions" in styles
    assert ".asset-viewer-copy-clean" in styles
    assert ".asset-viewer-download-clean" in styles
    assert ".asset-viewer-more-body" in styles
    assert ".asset-viewer-previous" in styles
    assert ".asset-viewer-next" in styles
    assert ".viewer-marked-for-exclusion" in styles
    assert "env(safe-area-inset-bottom)" in styles
    assert "@media (max-width: 680px)" in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles


def test_review_filter_hides_empty_ai_excluded_separator() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")

    assert "updateExcludedHeadings" in script
    assert 'document.querySelectorAll(".ai-excluded-heading")' in script
    assert "grid.querySelectorAll('.asset-card[data-ai-excluded=\"true\"]')" in script
    assert "heading.hidden = !hasVisibleExcluded" in script
