from __future__ import annotations

from gen_automation.domain.storybooks import (
    StorybookNormalizedBox,
    StorybookOverlay,
    StorybookOverlayKind,
    StorybookOverlayStyle,
    StorybookPagePlan,
    StorybookPlacementHint,
)
from gen_automation.services.storybook_layout import (
    layout_storybook_page,
    render_storybook_overlay_svg_preview,
)


def _page() -> StorybookPagePlan:
    return StorybookPagePlan(
        page_number=1,
        scene_summary="Two friends notice a paper airplane crossing the room.",
        overlays=(
            StorybookOverlay(
                element_id="first-line",
                kind=StorybookOverlayKind.DIALOGUE,
                text="Did you see where that came from?",
                reading_order=1,
                style=StorybookOverlayStyle.CLASSIC_LIGHT,
                speaker_key="a",
                placement_hint=StorybookPlacementHint.TOP_LEFT,
            ),
            StorybookOverlay(
                element_id="paper-sfx",
                kind=StorybookOverlayKind.SFX,
                text="FWIP!",
                reading_order=2,
                style=StorybookOverlayStyle.IMPACT_SFX,
            ),
        ),
    )


def test_layout_is_deterministic_and_avoids_a_protected_face_region() -> None:
    page = _page()
    protected = (StorybookNormalizedBox(x=0, y=0, width=470_000, height=390_000),)
    first = layout_storybook_page(page, protected_regions=protected)
    second = layout_storybook_page(page, protected_regions=protected)

    assert first == second
    assert first.layout_sha256 == second.layout_sha256
    first_box = first.placements[0].box
    assert first_box.x >= 470_000 or first_box.y >= 390_000
    assert first.manual_review_required is False


def test_crowded_layout_fails_visible_for_manual_review() -> None:
    layout = layout_storybook_page(
        _page(),
        protected_regions=(StorybookNormalizedBox(x=0, y=0, width=1_000_000, height=1_000_000),),
    )

    assert layout.manual_review_required is True
    assert all(item.manual_review_required for item in layout.placements)


def test_long_copy_requests_review_instead_of_becoming_tiny_or_clipped() -> None:
    page = StorybookPagePlan(
        page_number=1,
        scene_summary="A safe but deliberately verbose dialogue test.",
        overlays=(
            StorybookOverlay(
                element_id="long-copy",
                kind=StorybookOverlayKind.DIALOGUE,
                text=" ".join(["conversation"] * 24),
                reading_order=1,
                style=StorybookOverlayStyle.CLASSIC_LIGHT,
                speaker_key="a",
            ),
        ),
    )

    layout = layout_storybook_page(page)

    assert layout.manual_review_required is True
    assert layout.placements[0].font_size_micros >= 24_000


def test_svg_preview_escapes_copy_and_uses_vector_lettering_presets() -> None:
    page = StorybookPagePlan(
        page_number=1,
        scene_summary="A harmless lettering test.",
        overlays=(
            StorybookOverlay(
                element_id="safe-copy",
                kind=StorybookOverlayKind.DIALOGUE,
                text="Tea & snacks < later",
                reading_order=1,
                style=StorybookOverlayStyle.CLASSIC_INVERSE,
                speaker_key="a",
            ),
            StorybookOverlay(
                element_id="accent-copy",
                kind=StorybookOverlayKind.DIALOGUE,
                text="Wait for me!",
                reading_order=2,
                style=StorybookOverlayStyle.ACCENT_FLOAT,
                speaker_key="b",
                accent_color="#EF6BD2",
            ),
        ),
    )
    layout = layout_storybook_page(page)
    svg = render_storybook_overlay_svg_preview(page, layout, width=1024, height=1536)

    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    assert "Tea &amp; snacks &lt; later" in svg
    assert "Tea & snacks < later" not in svg
    assert 'fill="#101016"' in svg
    assert 'fill="#EF6BD2"' in svg
    assert "<script" not in svg


def test_svg_preview_rejects_layout_from_another_page() -> None:
    page = _page()
    layout = layout_storybook_page(page).model_copy(update={"page_number": 2})

    try:
        render_storybook_overlay_svg_preview(page, layout, width=1024, height=1536)
    except ValueError as error:
        assert str(error) == "storybook preview layout does not match the page"
    else:
        raise AssertionError("mismatched storybook layout was accepted")
