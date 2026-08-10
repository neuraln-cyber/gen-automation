from __future__ import annotations

import hashlib
import html
import textwrap
from collections.abc import Mapping, Sequence

from gen_automation.domain.storybooks import (
    NORMALIZED_SCALE,
    StorybookNormalizedBox,
    StorybookNormalizedPoint,
    StorybookOverlay,
    StorybookOverlayKind,
    StorybookOverlayPlacement,
    StorybookOverlayStyle,
    StorybookPageLayout,
    StorybookPagePlan,
    StorybookPlacementHint,
)

_SAFE_MARGIN = 50_000
_MIN_GAP = 16_000
_MAX_PREVIEW_SVG_BYTES = 256 * 1024


def layout_storybook_page(
    page: StorybookPagePlan,
    *,
    protected_regions: Sequence[StorybookNormalizedBox] = (),
    speaker_regions: Mapping[str, StorybookNormalizedBox] | None = None,
) -> StorybookPageLayout:
    """Place one page's overlays without model calls or raster decoding.

    Protected regions normally come from a later face/focal-region detector. A
    non-colliding candidate is preferred; crowded pages are returned with an
    explicit manual-review flag instead of silently making text unreadable.
    """

    speakers = speaker_regions or {}
    placed_boxes: list[StorybookNormalizedBox] = []
    placements: list[StorybookOverlayPlacement] = []
    page_requires_review = False

    for overlay in sorted(page.overlays, key=lambda item: item.reading_order):
        lines, font_size, width, height, copy_requires_review = _measure_overlay(overlay)
        candidates = _candidate_boxes(width=width, height=height)
        candidates = _ordered_candidates(candidates, hint=overlay.placement_hint)
        selected = min(
            enumerate(candidates),
            key=lambda item: _placement_score(
                item[1],
                rank=item[0],
                protected_regions=protected_regions,
                placed_regions=placed_boxes,
            ),
        )[1]
        collision_area = sum(_overlap_area(selected, item) for item in protected_regions)
        collision_area += sum(_overlap_area(selected, item) for item in placed_boxes)
        requires_review = collision_area > 0 or copy_requires_review
        page_requires_review = page_requires_review or requires_review

        tail_target = None
        if overlay.speaker_key is not None and overlay.speaker_key in speakers:
            speaker = speakers[overlay.speaker_key]
            tail_target = StorybookNormalizedPoint(
                x=speaker.x + speaker.width // 2,
                y=speaker.y + speaker.height // 2,
            )
        placements.append(
            StorybookOverlayPlacement(
                element_id=overlay.element_id,
                box=selected,
                text_lines=lines,
                font_size_micros=font_size,
                rotation_millidegrees=_rotation(overlay),
                tail_target=tail_target,
                manual_review_required=requires_review,
            )
        )
        placed_boxes.append(_expanded_box(selected, _MIN_GAP))

    return StorybookPageLayout(
        page_number=page.page_number,
        placements=tuple(placements),
        manual_review_required=page_requires_review,
    )


def render_storybook_overlay_svg_preview(
    page: StorybookPagePlan,
    layout: StorybookPageLayout,
    *,
    width: int,
    height: int,
) -> str:
    """Render a bounded vector preview.

    This intentionally uses generic browser font families. The production
    raster compositor will require bundled, digest-pinned font files while
    consuming the same normalized layout contract.
    """

    if (
        isinstance(width, bool)
        or isinstance(height, bool)
        or not isinstance(width, int)
        or not isinstance(height, int)
        or width < 64
        or height < 64
    ):
        raise ValueError("storybook preview dimensions are invalid")
    if width > 16_384 or height > 16_384 or width * height > 64_000_000:
        raise ValueError("storybook preview dimensions exceed the safety limit")
    if layout.page_number != page.page_number:
        raise ValueError("storybook preview layout does not match the page")
    overlays = {overlay.element_id: overlay for overlay in page.overlays}
    if set(overlays) != {placement.element_id for placement in layout.placements}:
        raise ValueError("storybook preview layout elements do not match the page")

    body = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" role="img" '
            f'aria-label="Story page {page.page_number} lettering preview">'
        ),
        "<style>",
        ".story-dialogue{font-family:'Comic Neue','Trebuchet MS',sans-serif;font-weight:700}",
        (
            ".story-expressive{font-family:'Kalam','Segoe Print',cursive;"
            "font-weight:700;font-style:italic}"
        ),
        ".story-impact{font-family:'Bangers','Impact',sans-serif;font-weight:900;letter-spacing:.03em}",
        ".story-narration{font-family:Inter,system-ui,sans-serif;font-weight:750}",
        "text{paint-order:stroke fill;stroke-linejoin:round}",
        "</style>",
    ]
    for placement in layout.placements:
        body.extend(
            _svg_element(
                overlays[placement.element_id],
                placement,
                width=width,
                height=height,
            )
        )
    body.append("</svg>")
    rendered = "".join(body)
    if len(rendered.encode("utf-8")) > _MAX_PREVIEW_SVG_BYTES:
        raise ValueError("storybook preview SVG exceeds the safety limit")
    return rendered


def _measure_overlay(
    overlay: StorybookOverlay,
) -> tuple[tuple[str, ...], int, int, int, bool]:
    if overlay.kind is StorybookOverlayKind.SFX:
        wrap_width = 13
        base_font = 62_000
        min_width = 220_000
        max_width = 520_000
    elif overlay.kind is StorybookOverlayKind.NARRATION:
        wrap_width = 34
        base_font = 30_000
        min_width = 300_000
        max_width = 700_000
    else:
        wrap_width = 24
        base_font = 39_000
        min_width = 260_000
        max_width = 520_000

    lines = tuple(
        textwrap.wrap(
            overlay.text,
            width=wrap_width,
            break_long_words=False,
            break_on_hyphens=False,
        )
    ) or (overlay.text,)
    longest_line = max(len(line) for line in lines)
    line_count = len(lines)
    font_size = max(24_000, base_font - max(0, line_count - 4) * 3_000)
    width = _clamp(
        70_000 + longest_line * font_size * 58 // 100,
        minimum=min_width,
        maximum=max_width,
    )
    height = _clamp(
        64_000 + line_count * font_size * 13 // 10,
        minimum=110_000,
        maximum=390_000,
    )
    requires_review = line_count > 7 or longest_line > wrap_width * 2
    return lines, font_size, width, height, requires_review


def _candidate_boxes(*, width: int, height: int) -> tuple[StorybookNormalizedBox, ...]:
    left = _SAFE_MARGIN
    center = (NORMALIZED_SCALE - width) // 2
    right = NORMALIZED_SCALE - _SAFE_MARGIN - width
    top = _SAFE_MARGIN
    middle = (NORMALIZED_SCALE - height) // 2
    bottom = NORMALIZED_SCALE - _SAFE_MARGIN - height
    coordinates = (
        (left, top),
        (center, top),
        (right, top),
        (left, middle),
        (right, middle),
        (left, bottom),
        (center, bottom),
        (right, bottom),
    )
    return tuple(
        StorybookNormalizedBox(x=x, y=y, width=width, height=height) for x, y in coordinates
    )


def _ordered_candidates(
    candidates: tuple[StorybookNormalizedBox, ...],
    *,
    hint: StorybookPlacementHint,
) -> tuple[StorybookNormalizedBox, ...]:
    if hint is StorybookPlacementHint.AUTO:
        return candidates
    index_by_hint = {
        StorybookPlacementHint.TOP_LEFT: 0,
        StorybookPlacementHint.TOP_CENTER: 1,
        StorybookPlacementHint.TOP_RIGHT: 2,
        StorybookPlacementHint.MIDDLE_LEFT: 3,
        StorybookPlacementHint.MIDDLE_RIGHT: 4,
        StorybookPlacementHint.BOTTOM_LEFT: 5,
        StorybookPlacementHint.BOTTOM_CENTER: 6,
        StorybookPlacementHint.BOTTOM_RIGHT: 7,
    }
    preferred = index_by_hint[hint]
    return (
        candidates[preferred],
        *(item for index, item in enumerate(candidates) if index != preferred),
    )


def _placement_score(
    candidate: StorybookNormalizedBox,
    *,
    rank: int,
    protected_regions: Sequence[StorybookNormalizedBox],
    placed_regions: Sequence[StorybookNormalizedBox],
) -> int:
    protected_overlap = sum(_overlap_area(candidate, item) for item in protected_regions)
    placed_overlap = sum(_overlap_area(candidate, item) for item in placed_regions)
    return protected_overlap * 12 + placed_overlap * 20 + rank


def _overlap_area(left: StorybookNormalizedBox, right: StorybookNormalizedBox) -> int:
    overlap_width = max(0, min(left.x + left.width, right.x + right.width) - max(left.x, right.x))
    overlap_height = max(
        0,
        min(left.y + left.height, right.y + right.height) - max(left.y, right.y),
    )
    return overlap_width * overlap_height


def _expanded_box(box: StorybookNormalizedBox, amount: int) -> StorybookNormalizedBox:
    x = max(0, box.x - amount)
    y = max(0, box.y - amount)
    right = min(NORMALIZED_SCALE, box.x + box.width + amount)
    bottom = min(NORMALIZED_SCALE, box.y + box.height + amount)
    return StorybookNormalizedBox(x=x, y=y, width=right - x, height=bottom - y)


def _rotation(overlay: StorybookOverlay) -> int:
    if overlay.style not in {
        StorybookOverlayStyle.IMPACT_SFX,
        StorybookOverlayStyle.ACCENT_FLOAT,
        StorybookOverlayStyle.SOFT_CLOUD,
    }:
        return 0
    value = int.from_bytes(hashlib.sha256(overlay.element_id.encode()).digest()[:2], "big")
    return (value % 12_001) - 6_000


def _svg_element(
    overlay: StorybookOverlay,
    placement: StorybookOverlayPlacement,
    *,
    width: int,
    height: int,
) -> list[str]:
    x = _pixels(placement.box.x, width)
    y = _pixels(placement.box.y, height)
    box_width = _pixels(placement.box.width, width)
    box_height = _pixels(placement.box.height, height)
    center_x = x + box_width / 2
    center_y = y + box_height / 2
    rotation = placement.rotation_millidegrees / 1_000
    parts = [
        (
            f'<g data-story-element="{overlay.element_id}" '
            f'transform="rotate({rotation:.3f} {center_x:.3f} {center_y:.3f})">'
        )
    ]
    parts.extend(
        _svg_backing(
            overlay,
            placement,
            x=x,
            y=y,
            width=box_width,
            height=box_height,
            canvas_width=width,
            canvas_height=height,
        )
    )
    text_class, fill, stroke, stroke_width = _text_style(overlay)
    font_size = max(12.0, placement.font_size_micros * min(width, height) / NORMALIZED_SCALE)
    line_height = font_size * 1.18
    first_y = center_y - (len(placement.text_lines) - 1) * line_height / 2
    parts.append(
        f'<text class="{text_class}" x="{center_x:.3f}" y="{first_y:.3f}" '
        f'text-anchor="middle" dominant-baseline="middle" font-size="{font_size:.3f}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{stroke_width:.3f}">'
    )
    for index, line in enumerate(placement.text_lines):
        dy = 0 if index == 0 else line_height
        parts.append(f'<tspan x="{center_x:.3f}" dy="{dy:.3f}">{html.escape(line)}</tspan>')
    parts.extend(("</text>", "</g>"))
    return parts


def _svg_backing(
    overlay: StorybookOverlay,
    placement: StorybookOverlayPlacement,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    canvas_width: int,
    canvas_height: int,
) -> list[str]:
    style = overlay.style
    if style in {StorybookOverlayStyle.ACCENT_FLOAT, StorybookOverlayStyle.IMPACT_SFX}:
        return []
    if style is StorybookOverlayStyle.CLASSIC_LIGHT:
        fill, stroke, opacity = "#FFFDF7", "#101016", "0.98"
    elif style is StorybookOverlayStyle.CLASSIC_INVERSE:
        fill, stroke, opacity = "#101016", "#FFFDF7", "0.98"
    elif style is StorybookOverlayStyle.SOFT_CLOUD:
        fill, stroke, opacity = "#101016", "#101016", "0.88"
    elif style is StorybookOverlayStyle.THOUGHT_WHISPER:
        fill, stroke, opacity = "#FFFDF7", "#101016", "0.90"
    else:
        fill, stroke, opacity = "#101016", "#FFFDF7", "0.82"
    radius = min(width, height) * (0.48 if style is StorybookOverlayStyle.SOFT_CLOUD else 0.34)
    outline_width = max(2.0, min(canvas_width, canvas_height) * 0.003)
    parts = [
        f'<rect x="{x:.3f}" y="{y:.3f}" width="{width:.3f}" height="{height:.3f}" '
        f'rx="{radius:.3f}" fill="{fill}" fill-opacity="{opacity}" '
        f'stroke="{stroke}" stroke-width="{outline_width:.3f}"/>'
    ]
    if placement.tail_target is not None:
        target_x = _pixels(placement.tail_target.x, canvas_width)
        target_y = _pixels(placement.tail_target.y, canvas_height)
        base_y = y + height * 0.86
        base_left = x + width * 0.42
        base_right = x + width * 0.56
        parts.append(
            f'<path d="M {base_left:.3f} {base_y:.3f} L {target_x:.3f} {target_y:.3f} '
            f'L {base_right:.3f} {base_y:.3f} Z" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="{outline_width:.3f}"/>'
        )
    return parts


def _text_style(overlay: StorybookOverlay) -> tuple[str, str, str, float]:
    if overlay.style is StorybookOverlayStyle.CLASSIC_INVERSE:
        return "story-dialogue", "#FFFDF7", "#101016", 0.0
    if overlay.style is StorybookOverlayStyle.ACCENT_FLOAT:
        return "story-expressive", overlay.accent_color, "#101016", 5.0
    if overlay.style is StorybookOverlayStyle.SOFT_CLOUD:
        return "story-expressive", overlay.accent_color, "#101016", 1.5
    if overlay.style is StorybookOverlayStyle.THOUGHT_WHISPER:
        return "story-expressive", "#101016", "#FFFDF7", 1.0
    if overlay.style is StorybookOverlayStyle.IMPACT_SFX:
        return "story-impact", overlay.accent_color, "#101016", 7.0
    if overlay.style is StorybookOverlayStyle.NARRATION:
        return "story-narration", "#FFFDF7", "#101016", 1.0
    return "story-dialogue", "#101016", "#FFFDF7", 0.0


def _pixels(normalized: int, dimension: int) -> float:
    return normalized * dimension / NORMALIZED_SCALE


def _clamp(value: int, *, minimum: int, maximum: int) -> int:
    return min(maximum, max(minimum, value))
