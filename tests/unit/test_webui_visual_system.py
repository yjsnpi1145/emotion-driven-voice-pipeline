from __future__ import annotations

import re
from pathlib import Path

WEBUI = Path(__file__).parents[2] / "src" / "voice_pipeline" / "webui"


def _asset(name: str) -> str:
    return (WEBUI / name).read_text(encoding="utf-8")


def _rule(stylesheet: str, selector: str) -> str:
    match = re.search(rf"(?:^|\n){re.escape(selector)}\s*\{{([^}}]*)\}}", stylesheet)
    assert match is not None, f"missing CSS rule: {selector}"
    return match.group(1)


def test_global_design_tokens_and_focus_contract() -> None:
    stylesheet = _asset("styles.css")
    for token in (
        "--surface-canvas:",
        "--surface-panel:",
        "--surface-subtle:",
        "--surface-raised:",
        "--text-primary:",
        "--text-secondary:",
        "--text-tertiary:",
        "--interactive:",
        "--focus-ring:",
        "--space-1:",
        "--space-6:",
        "--text-xs:",
        "--text-xl:",
        "--radius-md:",
        "--control-height:",
        "--shadow-panel:",
    ):
        assert token in stylesheet
    assert "scroll-padding-top: var(--shell-offset)" in stylesheet
    focus_rule = _rule(
        stylesheet,
        "button:focus-visible, input:focus-visible, textarea:focus-visible, "
        "select:focus-visible, summary:focus-visible",
    )
    assert "var(--focus-ring)" in focus_rule
    assert "outline-offset: 2px" in focus_rule


def test_global_controls_use_shared_height_and_motion_tokens() -> None:
    stylesheet = _asset("styles.css")
    button_rule = _rule(stylesheet, "button")
    field_rule = _rule(stylesheet, "input, textarea, select")
    assert "min-height: var(--control-height)" in button_rule
    assert "var(--radius-control)" in button_rule
    assert "var(--motion-fast)" in button_rule
    assert "min-height: var(--control-height)" in field_rule
    assert "var(--radius-control)" in field_rule


def test_navigation_uses_local_accessible_svg_icons_and_new_cache_version() -> None:
    page = _asset("index.html")
    director_script = _asset("director.js")
    assert 'class="icon-sprite" aria-hidden="true"' in page
    assert page.count('class="tab-icon" aria-hidden="true"') == 5
    assert 'class="button-icon" aria-hidden="true"' in page
    for glyph in ("◉", "◎", "◆", "✦", "▦", "↻"):
        assert glyph not in page
    assert 'href="/ui/styles.css?v=20260829a"' in page
    assert 'src="/ui/app.js?v=20260829a"' in page
    assert 'src="/ui/director.js?v=20260829a"' in page
    assert 'from "./director-dnd.js?v=20260829a"' in director_script
    assert 'from "./director-adjustment.js?v=20260829a"' in director_script


def test_shared_panels_and_workbench_use_semantic_component_tokens() -> None:
    stylesheet = _asset("styles.css")
    panel_rule = _rule(stylesheet, ".panel")
    toolbar_rule = _rule(stylesheet, ".chapter-toolbar")
    selected_rule = _rule(stylesheet, '.segment-row[data-selected="true"]')
    scroll_rule = _rule(stylesheet, ".workbench > .panel")
    assert "var(--radius-lg)" in panel_rule
    assert "var(--shadow-panel)" in panel_rule
    assert "var(--radius-lg)" in toolbar_rule
    assert "var(--space-4)" in toolbar_rule
    assert "inset 3px 0 var(--interactive)" in selected_rule
    assert "scrollbar-gutter: stable" in scroll_rule


def test_workbench_keeps_three_two_one_column_progression() -> None:
    stylesheet = _asset("styles.css")
    assert re.search(
        r"\.workbench\s*\{[^}]*grid-template-columns:\s*"
        r"minmax\(17rem,\s*\.82fr\)\s+minmax\(20rem,\s*1fr\)\s+"
        r"minmax\(30rem,\s*1\.65fr\)",
        stylesheet,
    )
    assert "@media (max-width: 1220px)" in stylesheet
    assert "@media (max-width: 820px)" in stylesheet
    assert ".chapter-action-buttons" in stylesheet
