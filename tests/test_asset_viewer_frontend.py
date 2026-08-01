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
    assert 'event.key === "Escape"' in script
    assert "returnFocus.focus()" in script
    assert "new Image()" in script
    assert "visibleCards()" in script
    assert "Download exact raw master" in script
    assert "assetViewerSelect" in script
    assert 'selection.dispatchEvent(new Event("change", { bubbles: true }))' in script
    assert "assetViewerSettings" in script
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
    assert "env(safe-area-inset-bottom)" in styles
    assert "@media (max-width: 680px)" in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles


def test_review_filter_hides_empty_ai_excluded_separator() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")

    assert "updateExcludedHeadings" in script
    assert 'document.querySelectorAll(".ai-excluded-heading")' in script
    assert 'grid.querySelectorAll(\'.asset-card[data-ai-excluded="true"]\')' in script
    assert "heading.hidden = !hasVisibleExcluded" in script
