# tests/unit/hygiene/test_visual_system.py
# Responsibility: Keep the console's fonts and palette on the values hexera.ai actually ships.
# Boundaries: a read-only repository gate; it renders nothing and starts no browser.
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).parents[3]
CSS = REPO / "ui" / "css"
LEGACY_STYLES = REPO / "apps" / "console" / "src" / "app" / "_components" / "legacy-styles.tsx"


def test_the_console_requests_the_display_weight_the_site_uses():
    # Saira 300 is the site's display voice -- tall and thin. Requesting 400 as the lightest
    # weight is what made the console's headings read as a generic dark dashboard.
    request = LEGACY_STYLES.read_text()
    assert "Saira+Semi+Condensed:wght@300;400;500;600" in request
    assert "Geist:wght@300;400;500;600" in request
    assert "Geist+Mono:wght@400;500" in request


def test_the_canvas_is_anchored_on_the_sites_own_black():
    tokens = (CSS / "tokens.css").read_text()
    assert "--canvas:#0d0d0c" in tokens.replace(" ", "")


def test_the_chrome_carries_the_sites_primitives():
    chrome = (CSS / "chrome.css").read_text()
    for selector in (".page-bg", ".grain", ".nav", ".brand__word", ".nav__link",
                     ".btn--gold", ".btn--ghost"):
        assert selector in chrome, f"chrome.css is missing {selector}"


def test_the_page_bg_layers_two_glows_not_a_darkening_vignette():
    # hexera-site is not available in CI, so this cannot verify fidelity against the live site --
    # that gap is an accepted cost, not something this test pretends to close. What it CAN pin is
    # the shape of the primitive that was wrong once already: the site's own .page-bg (a still
    # background with two soft radial glows) got confused with its .vignette (a hero-only
    # darkening fade) in an earlier draft. Presence-only checks let that pass; this pins values.
    chrome = (CSS / "chrome.css").read_text()
    start = chrome.index(".page-bg{")
    block = chrome[start:start + 400]
    assert block.count("radial-gradient(") == 2, ".page-bg should layer exactly two glows"
    for darkening_literal in ("rgba(8,8,7", "rgba(6,6,5", "rgba(7,7,6", "rgba(11,11,10"):
        assert darkening_literal not in block, (
            f".page-bg still carries the vignette's darkening literal {darkening_literal}"
        )


def test_the_nav_bar_eases_into_its_stuck_state():
    chrome = (CSS / "chrome.css").read_text()
    start = chrome.index(".nav{")
    base_nav = chrome[start:start + 300]
    assert "transition" in base_nav, ".nav lost the transition that eases it into .is-stuck"


def test_the_console_ships_no_second_accent():
    # One hot colour. A stray hex that is neither the accent, the steel linework nor a status
    # role is how a palette becomes six colours nobody chose.
    banned = re.compile(r"#(?:ffba00|0fb6ac|0a8b84|ddab46|e8c879)", re.IGNORECASE)
    for sheet in CSS.glob("*.css"):
        found = banned.findall(sheet.read_text())
        assert not found, f"{sheet.name} carries a retired palette value: {found}"


def test_no_stylesheet_reaches_for_the_hero_canvas():
    # Decision 3: the console is .page-bg + .grain, like the site's own contact page. fluid.js is
    # 40 KB and the console already ships vtk.js.
    for sheet in CSS.glob("*.css"):
        assert "fluid" not in sheet.read_text().lower(), f"{sheet.name} references the hero canvas"


def test_the_auth_surface_defines_the_classes_its_pages_consume():
    auth = (CSS / "auth.css").read_text()
    for selector in (".auth__panel", ".auth__title", ".auth__form", ".auth__field",
                     ".auth__input", ".auth__error", ".auth__alt"):
        assert selector in auth, f"auth.css is missing {selector}"


def test_the_auth_title_uses_the_display_face_at_its_thin_weight():
    auth = (CSS / "auth.css").read_text()
    title = auth[auth.index(".auth__title"):auth.index(".auth__title") + 400]
    assert "var(--display)" in title
    assert "font-weight:300" in title.replace(" ", "")


def test_the_dashboard_shell_defines_the_classes_its_pages_consume():
    dashboard = (CSS / "dashboard.css").read_text()
    for selector in (".shell", ".sidebar__nav", ".sidebar__item--current",
                     ".sidebar__item--unavailable", ".page__title", ".table", ".status--fail",
                     ".empty"):
        assert selector in dashboard, f"dashboard.css is missing {selector}"


def test_main_js_accepts_a_server_injected_job_as_well_as_the_query_string():
    main = (REPO / "ui" / "js" / "main.js").read_text()
    assert "__HEXERA_BOOT_JOB__" in main
    assert "__HEXERA_ROUTED__" in main
    # ui/index.html sets neither and must keep working exactly as it does today.
    assert 'params.get("job")' in main


def test_the_legacy_component_sheets_keep_the_selectors_main_js_drives():
    # main.js, stage.js and viewer.js select these at runtime. Restyling moves values; deleting a
    # selector silently breaks the workbench in a way no unit test would catch.
    required = {
        "shell.css": ("#upload-bar", "#stage", ".chat-col", "#notice"),
        "chat.css": (".im",),
        "workbench.css": ("#workbench",),
    }
    for filename, selectors in required.items():
        text = (CSS / filename).read_text()
        for selector in selectors:
            assert selector in text, f"{filename} lost {selector}"
