from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "src" / "gen_automation" / "static" / "tag_autocomplete.js"
STYLES = ROOT / "src" / "gen_automation" / "static" / "dashboard_ux.css"
BASE = ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "base.html"
NEW_SET = ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "new_set.html"
EXPERIMENT = ROOT / "src" / "gen_automation" / "templates" / "dashboard" / "experiment_new.html"


def test_all_generation_prompt_editors_opt_in_without_serialized_plans() -> None:
    new_set = NEW_SET.read_text(encoding="utf-8")
    experiment = EXPERIMENT.read_text(encoding="utf-8")
    base = BASE.read_text(encoding="utf-8")

    assert new_set.count("data-danbooru-autocomplete") == 10
    assert experiment.count("data-danbooru-autocomplete") == 6
    assert 'data-batch-field="prompt"\n            data-danbooru-autocomplete' in new_set
    assert 'data-batch-field="negative_prompt"\n            data-danbooru-autocomplete' in new_set
    assert "data-experiment-plan data-danbooru-autocomplete" not in experiment
    assert 'batch-plan-data" data-danbooru-autocomplete' not in new_set
    assert '<script src="/static/tag_autocomplete.js" defer></script>' in base


def test_autocomplete_is_delegated_cached_bounded_and_prompt_safe() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert 'const FIELD_SELECTOR = "textarea[data-danbooru-autocomplete]"' in script
    assert 'const ENDPOINT = "/api/v1/danbooru-tags/autocomplete"' in script
    assert "const DEBOUNCE_MILLISECONDS = 240" in script
    assert "const CLIENT_CACHE_LIMIT = 100" in script
    assert "new AbortController()" in script
    assert 'url.searchParams.set("q", token.query)' in script
    assert 'url.searchParams.set("limit", String(RESULT_LIMIT))' in script
    assert 'credentials: "same-origin"' in script
    assert "clientCache.size > CLIENT_CACHE_LIMIT" in script
    assert 'lowered.startsWith("__")' in script
    assert 'lowered.includes("<lora:")' in script
    assert 'fragment.includes(":")' not in script
    assert "/:(?:-?\\d+(?:\\.\\d*)?|\\.\\d+)$/u.test(fragment.trim())" in script
    assert '.replace(/\\\\([()[\\]{}])/gu, "$1")' in script
    assert 'name.replace(/[()[\\]]/gu, "\\\\$&")' in script
    assert 'field.setRangeText(replacement, token.start, token.end, "end")' in script
    assert "const suppressedInputs = new WeakSet()" in script
    assert "suppressedInputs.add(field)" in script
    assert "suppressedInputs.delete(event.target)" in script
    assert "document.activeElement !== event.target" in script
    assert "event.isTrusted" not in script
    assert 'field.dispatchEvent(new Event("input", { bubbles: true }))' in script
    assert "nextLength > field.maxLength" in script
    assert "textContent" in script
    assert "innerHTML" not in script


def test_autocomplete_supports_keyboard_touch_aria_and_mobile_layout() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")

    assert 'listbox.setAttribute("role", "listbox")' in script
    assert 'item.setAttribute("role", "option")' in script
    assert 'field.setAttribute("aria-autocomplete", "list")' in script
    assert 'field.setAttribute("role", "combobox")' in script
    assert 'field.setAttribute("aria-expanded", "true")' in script
    assert 'event.key === "ArrowDown"' in script
    assert 'event.key === "ArrowUp"' in script
    assert 'event.key === "Enter" || event.key === "Tab"' in script
    assert 'event.key === "Escape"' in script
    assert 'event.code === "Space"' in script
    assert 'popup.addEventListener("pointerdown"' in script
    assert 'event.pointerType === "mouse"' in script
    assert 'popup.addEventListener("click"' in script
    assert 'document.addEventListener("select"' in script
    assert 'window.addEventListener("scroll", schedulePosition, true)' in script
    assert ".danbooru-autocomplete" in styles
    assert "position: fixed" in styles
    assert '.danbooru-autocomplete-option[data-category="character"]' in styles
    assert "@media (max-width: 560px)" in styles
    assert "min-height: 3rem" in styles
