# Responsibility: Keep the two shipped copies of the browser UI - ui/ and the console's vendored
# static/ - byte-identical, so CI over one actually gates both.
# Boundaries: a read-only repository gate; it builds nothing and calls no cloud.
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[3]
UI = REPO / "ui"
CONSOLE_STATIC = REPO / "apps" / "console" / "public" / "static"

# ui/index.html is the legacy server-rendered mount point; the console has its own Next.js pages
# instead and carries no counterpart. Every other file under ui/ is the same browser copy the
# console's public/static/ vendors - see design-spec decision 9: shipping the console "stops the
# drift getting worse by putting the console copy under CI", which is true only if the two trees
# are actually kept identical. They are not otherwise checked anywhere else, so this is that check.
_NO_CONSOLE_COUNTERPART = {"index.html"}


def _relative_files(base: Path) -> set[str]:
    return {str(p.relative_to(base)) for p in base.rglob("*") if p.is_file()}


def test_the_console_static_copy_is_byte_identical_to_ui():
    ui_files = _relative_files(UI) - _NO_CONSOLE_COUNTERPART
    console_files = _relative_files(CONSOLE_STATIC)

    only_in_ui = sorted(ui_files - console_files)
    only_in_console = sorted(console_files - ui_files)
    assert not only_in_ui and not only_in_console, (
        "ui/ and apps/console/public/static/ do not list the same files - "
        f"only in ui/: {only_in_ui}; only in the console copy: {only_in_console}"
    )

    drifted = [
        rel for rel in sorted(ui_files)
        if (UI / rel).read_bytes() != (CONSOLE_STATIC / rel).read_bytes()
    ]
    assert not drifted, (
        "ui/ and apps/console/public/static/ have drifted - these files differ byte-for-byte: "
        f"{drifted}. The console ships its own copy of the browser UI rather than serving ui/ "
        "directly, so a fix applied to one silently does not reach the other; backport the "
        "change into whichever tree is missing it so the two stay identical (equivalent to: "
        "diff -rq ui apps/console/public/static, ignoring ui/index.html)."
    )
