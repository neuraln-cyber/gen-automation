from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_asset_cards_offer_lazy_generation_settings_with_json_fallback() -> None:
    for template_name in ("release_detail.html", "review_task.html"):
        template = (
            ROOT / "src" / "gen_automation" / "templates" / "dashboard" / template_name
        ).read_text(encoding="utf-8")
        assert "data-generation-details" in template
        assert "Generation settings" in template
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
