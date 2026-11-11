# Responsibility: Verify SHSTAGE-8 on the FEA stage in a real browser: a shell's line carries ONE two-way choice,
# "Elements: Quads | Triangles", drawn as the stage's other two-way choices (Bonded | Contact); it starts where the
# server says; a flip away from the user's settings or notes asks one short question first ("Your settings say Quads.
# Use Triangles for this run?") - Yes keeps it for the run, No puts the switch back - and a flip with nothing to
# disagree with is taken at once; the click survives the shell line being drawn again; Proceed sends what was settled.
# No control for a solid or a shell that cannot be made.
# Boundaries: the shell views are the server's shapes (application/shell_elements.view), built here; only the network
# is stood in for.
from __future__ import annotations

import json

import pytest
from conftest import assert_clean
from test_fea_stage_mesh_as import _check, _open, _proceed, _ready, _shell

from meshpipeline.application import shell_elements as SE

pytestmark = pytest.mark.ui


def _with(view: dict, standard: dict | None = None, said: str = "") -> dict:
    msgs = [{"role": "user", "content": said}] if said else []
    return {**view, "elements": SE.view(view, msgs, standard=standard)}


def _el(live, sid: str) -> dict:
    return live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{sid}');
      const box = root.querySelector('.fea-sec-shell .fea-el');
      const ask = root.querySelector('.fea-el-ask');
      return {{shown: !!box, text: box ? box.textContent.trim() : '',
               inHead: !!root.querySelector('.fea-sec-shell .fea-sec-head .fea-el'),
               seg: !!root.querySelector('.fea-sec-shell .fea-el .fea-seg'),
               checked: (root.querySelector('input[name="fea-shell-elements"]:checked') || {{}}).value || '',
               ask: ask ? ask.querySelector('span').textContent : ''}}; }})()""")


def _flip(live, sid: str, value: str) -> None:
    live.evaluate(f"""document.querySelector('#gstage-{sid} input[name="fea-shell-elements"][value="{value}"]')
      .closest('label').click()""")


def _answer(live, sid: str, yes: bool) -> None:
    live.evaluate(f"document.querySelector('#gstage-{sid} .fea-el-{'yes' if yes else 'no'}').click()")


def test_a_shell_line_carries_one_quads_or_triangles_choice_and_a_free_flip(live):
    """No settings, no notes: Quads; a flip to Triangles asks nothing, survives a redraw and is sent at Proceed."""
    sid = "elfree"
    _open(live, sid, _check(_with(_shell("measuring"))))
    s = _el(live, sid)
    assert s["shown"] and s["inHead"] and s["seg"] and s["text"].split() == ["Elements", "Quads", "Triangles"]
    assert s["checked"] == "quads" and s["ask"] == ""
    _flip(live, sid, "triangles")
    assert _el(live, sid)["checked"] == "triangles" and _el(live, sid)["ask"] == ""
    # the shell check lands and the line is drawn again: the click stays the user's
    live.evaluate(f"window.__stages['{sid}'].update({json.dumps(_check(_with(_ready())))})")
    live.wait_for(f"window._vdbg['gstage:{sid}'].shell().shown", timeout=30, what="the mid-surface to be drawn")
    assert _el(live, sid)["checked"] == "triangles"
    assert _proceed(live, sid)["fea_shell_elements"] == "triangles"
    assert_clean(live, "a free element choice")


def test_a_flip_away_from_the_settings_asks_first_and_no_puts_it_back(live):
    sid = "elsettings"
    std = {"text": "Global size 10 mm"}             # a standard, its switch on Quads
    _open(live, sid, _check(_with(_ready(), std)))
    assert _el(live, sid)["checked"] == "quads"
    _flip(live, sid, "triangles")
    s = _el(live, sid)
    assert s["ask"] == "Your settings say Quads. Use Triangles for this run?" and s["checked"] == "triangles"
    _answer(live, sid, yes=False)
    s = _el(live, sid)
    assert s["ask"] == "" and s["checked"] == "quads"
    _flip(live, sid, "triangles")
    _answer(live, sid, yes=True)
    s = _el(live, sid)
    assert s["ask"] == "" and s["checked"] == "triangles"
    assert _proceed(live, sid)["fea_shell_elements"] == "triangles"
    assert_clean(live, "a flip away from the settings")


def test_the_stage_starts_on_the_settings_switch_and_on_the_notes(live):
    sid = "elstart"
    _open(live, sid, _check(_with(_ready(), {"text": "", "shell_elements": "triangles"})))
    assert _el(live, sid)["checked"] == "triangles"
    _flip(live, sid, "quads")
    assert _el(live, sid)["ask"] == "Your settings say Triangles. Use Quads for this run?"
    # a question left open is not the user's choice yet: Proceed sends where it started
    assert _proceed(live, sid)["fea_shell_elements"] == "triangles"
    sid = "elnotes"
    _open(live, sid, _check(_with(_ready(), {"text": "x"}, said="shell mesh, use triangles for shells")))
    assert _el(live, sid)["checked"] == "triangles"
    _flip(live, sid, "quads")
    assert _el(live, sid)["ask"] == "Your notes say Triangles. Use Quads for this run?"
    assert_clean(live, "the stage starting on settings and notes")


def test_no_choice_without_a_shell(live):
    sid = "elnone"
    _open(live, sid, _check(None))
    assert not _el(live, sid)["shown"]
    assert "fea_shell_elements" not in _proceed(live, sid)
    sid = "elsolid"
    _open(live, sid, _check(_shell("ready", mesh_as="solid", label="Solid", reason="No plate here.")))
    assert not _el(live, sid)["shown"]
    assert_clean(live, "a stage with no shell choice")
