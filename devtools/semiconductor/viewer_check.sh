#!/bin/bash
# WHAT CAN BE CHECKED ABOUT THE VIEWER WITHOUT A BROWSER, which is not much, and saying so is the
# point of this script.
#
# THERE IS A BROWSER GATE AND IT IS STRICT. `tests/ui/` drives the shipped page in a real Chrome
# over CDP, and with no browser on PATH it FAILS rather than skips, on purpose: "a release must not
# pass with its browser validation quietly absent." Five of its files import `openViewer` from
# /static/js/viewer/viewer.js and drive `window._vdbg[...]` directly - test_heatmap, test_browser,
# test_fresh_downloads, test_a_merged_view_says_it_is_merged, test_thermal_results. 143 tests,
# about four minutes.
#
# An earlier version of this header said nothing in the repository runs viewer.js. That was wrong.
# What is true is narrower: Chrome is not installed in the WSL checkout, so the gate shows up as
# ~142 errors there and reads like noise, and it does not cover the DETAIL WINDOW - no file in
# tests/ui mentions detailWindow, v-detail or radiusNow. That is exactly where three defects
# shipped on 2026-10-29, all found by opening the page by hand:
#
#   * `tanHalf` was a const in one closure and called from a sibling one, so every click of the
#     Detail button threw ReferenceError. A feature with a route, a bounded reader, a face budget,
#     a button and thirty Python tests, that had never run.
#   * a truncated detail window dropped a whole region.
#   * painting a metric with the detail open coloured a surface the detail was hiding.
#
# To run the real gate here, install the browser the way the gate's own message says - the
# `chromium` apt package is a snap wrapper and will not run in WSL:
#
#   curl -fsSLo /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
#   sudo apt-get install -y /tmp/chrome.deb
#   .venv/bin/python -m pytest tests/ui -q
#
# So this script checks the two things that need no browser at all, for a quick loop: both copies
# PARSE, and both still have the entry points, because they drift.
set -u
cd "$(dirname "$0")/../.." || exit 1
UI=ui/js/viewer/viewer.js
CONSOLE=apps/console/public/static/js/viewer/viewer.js
NODE=${NODE:-node}
command -v "$NODE" >/dev/null 2>&1 || NODE="/mnt/c/Program Files/nodejs/node.exe"
rc=0

echo "== both copies parse as ES modules"
for f in "$UI" "$CONSOLE"; do
  tmp=$(mktemp /tmp/viewer_check_XXXXXX.mjs)
  cp "$f" "$tmp"
  if "$NODE" --check "$tmp" 2>/tmp/viewer_check_err; then
    echo "  OK      $f"
  else
    echo "  BROKEN  $f"; sed 's/^/          /' /tmp/viewer_check_err | head -5; rc=1
  fi
  rm -f "$tmp"
done

echo "== the entry points are in both copies"
for name in detailWindow heatmap radiusNow tanHalf closeDetail _vdbg; do
  a=$(grep -c "$name" "$UI"); b=$(grep -c "$name" "$CONSOLE")
  if [ "$a" -gt 0 ] && [ "$b" -gt 0 ]; then
    printf '  OK      %-14s ui %s, console %s\n' "$name" "$a" "$b"
  else
    printf '  MISSING %-14s ui %s, console %s\n' "$name" "$a" "$b"; rc=1
  fi
done

echo "== one definition of the shared camera helper, not one per closure"
for f in "$UI" "$CONSOLE"; do
  n=$(grep -c 'const tanHalf=' "$f")
  if [ "$n" -eq 1 ]; then
    echo "  OK      $f"
  else
    echo "  $n definitions in $f - a second one shadows the shared scope and the bug comes back"
    rc=1
  fi
done

echo "== how far apart the two copies are"
echo "  $(diff <(cat "$UI") <(cat "$CONSOLE") | grep -c '^[<>]') differing lines"

echo "== is the real gate runnable here?"
if command -v google-chrome >/dev/null 2>&1 || command -v chromium >/dev/null 2>&1; then
  echo "  yes - run it: .venv/bin/python -m pytest tests/ui -q"
else
  echo "  NO browser on PATH, so tests/ui will fail rather than skip. See the header."
fi

echo
echo "WHAT THIS DOES NOT CHECK: that any of it works. A name declared in the wrong closure parses"
echo "fine and throws on the click. Run tests/ui, and open the page."
exit $rc
