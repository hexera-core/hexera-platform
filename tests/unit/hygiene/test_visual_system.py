# tests/unit/hygiene/test_visual_system.py
# Responsibility: Keep the console's fonts and palette on the values hexera.ai actually ships.
# Boundaries: a read-only repository gate; it renders nothing and starts no browser.
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).parents[3]
CSS = REPO / "ui" / "css"
CONSOLE_SRC = REPO / "apps" / "console" / "src"
LEGACY_STYLES = CONSOLE_SRC / "app" / "_components" / "legacy-styles.tsx"
DASHBOARD_PAGES = CONSOLE_SRC / "app" / "(dashboard)"

_COMMENT = re.compile(r"//[^\n]*")
_IMPORT_SPEC = re.compile(r"""from\s+["']([^"']+)["']""")
_CLASS_USE = re.compile(r"\bauth__[A-Za-z0-9_-]+")


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


def _sheet_hrefs(name: str) -> list[str]:
    """The stylesheet URLs legacy-styles.tsx puts in `name`, spreads resolved.

    Read out of the source rather than restated here: a list this test keeps its own copy of
    would go on passing after somebody edited the real one, which is the exact failure below.
    """
    source = _COMMENT.sub("", LEGACY_STYLES.read_text())
    block = re.search(rf"const {name} = \[(.*?)\];", source, re.S)
    assert block, f"legacy-styles.tsx no longer declares {name}"
    hrefs: list[str] = []
    for spread, href in re.findall(r'\.\.\.(\w+)|"(/static/css/[^"]+)"', block.group(1)):
        hrefs.extend(_sheet_hrefs(spread) if spread else [href])
    return hrefs


def _module_for(spec: str, importer: Path) -> Path | None:
    if spec.startswith("@/"):
        base = CONSOLE_SRC / spec[2:]
    elif spec.startswith("."):
        base = importer.parent / spec
    else:
        return None                          # a package, not a file in this app
    for candidate in (Path(f"{base}.tsx"), Path(f"{base}.ts"), base / "index.tsx"):
        if candidate.is_file():
            return candidate.resolve()
    return None


def _modules_the_dashboard_renders() -> set[Path]:
    """Every source file a route under (dashboard) reaches, transitively.

    The classes that went unstyled were not IN the pages -- they are in `account-form.tsx` and
    `api-keys-panel.tsx`, which the pages import. Walking the import graph is what makes this
    guard see what the browser sees.
    """
    pending = [p.resolve() for p in DASHBOARD_PAGES.rglob("*.tsx")]
    assert pending, "no dashboard routes found -- this guard would pass vacuously"
    seen: set[Path] = set()
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        for spec in _IMPORT_SPEC.findall(module.read_text()):
            resolved = _module_for(spec, module)
            if resolved is not None:
                pending.append(resolved)
    return seen


def test_every_auth_class_a_dashboard_route_uses_is_in_a_sheet_that_route_loads():
    # THE GUARD THE OLD ONE WAS NOT. Asserting that auth.css DEFINES .auth__input says nothing
    # about whether the page using it ever downloads auth.css -- and it did not:
    # /settings/api-keys and /settings/account rendered browser-default inputs and an unstyled
    # error box because DASHBOARD_SHEETS omitted the sheet. What has to be true is that the
    # classes a route uses are defined in a sheet DashboardStyles actually lists.
    loaded = _sheet_hrefs("DASHBOARD_SHEETS")
    stylesheets = {}
    for href in loaded:
        sheet = CSS / Path(href).name
        assert sheet.is_file(), f"DASHBOARD_SHEETS lists {href}, which ui/css does not have"
        stylesheets[sheet.name] = sheet.read_text()

    used: dict[str, Path] = {}
    for module in _modules_the_dashboard_renders():
        for cls in _CLASS_USE.findall(module.read_text()):
            used.setdefault(cls, module)
    assert used, "no auth__ classes found under (dashboard) -- this guard would pass vacuously"

    for cls, module in sorted(used.items()):
        defined_in = [name for name, text in stylesheets.items()
                      if re.search(rf"\.{cls}\b", text)]
        assert defined_in, (
            f"{module.name} renders .{cls}, which no sheet in DASHBOARD_SHEETS defines -- "
            f"the dashboard loads {sorted(stylesheets)}"
        )


def test_the_dashboard_style_component_is_the_thing_that_lists_those_sheets():
    # _sheet_hrefs reads a const; this is what ties that const to the component the layout
    # renders, so renaming or re-pointing DashboardStyles cannot leave the guard above measuring
    # a list nothing uses.
    source = LEGACY_STYLES.read_text()
    body = source[source.index("export function DashboardStyles"):]
    assert "DASHBOARD_SHEETS" in body[:200], "DashboardStyles no longer renders DASHBOARD_SHEETS"
