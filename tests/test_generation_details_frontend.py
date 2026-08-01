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

    assert 'const upscalerEnabled = details.hires.enabled === true' in script
    assert '"Full-image upscaler"' in script
    assert '"Off - no upscale node in workflow"' in script
    assert 'if (upscalerEnabled)' in script


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
    assert 'option.dataset.upscalerEnabled === desired' in script
    assert 'workflow.dispatchEvent(new Event("change", { bubbles: true }))' in script
    assert "no upscale node or second sampler pass" in script
