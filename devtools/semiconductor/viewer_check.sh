#!/bin/bash
# WHAT CAN BE CHECKED ABOUT THE VIEWER WITHOUT A BROWSER, which is not much, and saying so is the
# point of this script.
#
# Three defects shipped in viewer.js and none of them could have been caught by anything in this
# repository (2026-10-29, all three found by opening the page):
#
#   * `tanHalf` was a const in one closure and called from a sibling one, so every click of the
#     Detail button threw ReferenceError. The feature had a route, a budget, a reader and twenty
#     Python tests, and had never run.
#   * a truncated detail window dropped a whole region.
#   * painting a metric with the detail open coloured a surface the detail was hiding.
#
# Every test of the viewer is Python, on the server side. Nothing runs viewer.js. The only JS
# linter in the tree is apps/console/eslint.config.mjs and its first rule is
# `ignores: ["public/static/**"]` - the directory one of the two copies lives in.
#
# So this checks the two things that do not need a browser: both copies PARSE, and both copies
# still have the entry points, because they drift (132 lines apart when last measured).
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

echo
echo "WHAT THIS DOES NOT CHECK: that any of it works. A name declared in the wrong closure parses"
echo "fine and throws on the click. Open the page."
exit $rc
