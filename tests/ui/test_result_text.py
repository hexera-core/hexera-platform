# Responsibility: Verify a finished run's closing message reads as a summary and loses no word on the way.
from __future__ import annotations

import json
import re

import pytest

pytestmark = pytest.mark.ui

#: The shapes final_result.render_message writes, one per ending.
CONCERNS = """Delivered with the reviewer's concerns:
  - IMPORTANT - Cells degrade where the mesh steps from coarse to fine
    Reviewer: The wall patch shows connected cut-cell fields. Nevertheless, maximum face non-orthogonality is 71.0953 degrees, above the 65 degree advisory tolerance; this is a material CFD mesh-quality concern.
    (the review asked for a rebuild for this; the rebuild could not satisfy it)
Every automatic check on the mesh passed - it is sound, its cells clear this engine's quality bars and every boundary you named is there with the right type - so it is delivered. The reviewer's points above are listed for you to weigh; look at those areas before you rely on the results, or tell me what to change for a new run.
Mesh generation completed successfully.
Engine: cfmesh
Mesh detail preference: Standard (user requested)
Required deliverable: ready to download
Review: delivered with concerns (see above)"""

PASSED = """Mesh generation completed successfully.
Engine: snappy
Mesh detail preference: Standard (system default)
Required deliverable: ready to download
Reviewer notes (an AI reviewer looked at the mesh; its notes are for you to weigh and do not decide the result):
  - Reviewer note: Wall layers: 4.71 of 5 layers on average over 97.3412% of the wall.
Review: passed"""

FAILED = """Mesh generation did not complete successfully.
The part has 2 bodies that touch but do not share faces, so the solid mesh would split at the contact. Merge them in your CAD, or tell me to treat them as separate parts, and run it again.
No downloadable mesh deliverable is available.
What each attempt did:
  - Attempt 1 (Gmsh): the volume mesh failed at the contact between the two bodies.
  - Attempt 2 (Netgen): the volume mesh failed at the contact between the two bodies."""


def _render(live, text: str, concerns: bool = False) -> dict:
    return live.evaluate("""(async () => {
      const { outcomeHtml } = await import('/static/js/render/outcome.js');
      const host = document.createElement('div');
      host.innerHTML = outcomeHtml(__TEXT__, {concerns: __CONCERNS__});
      document.body.appendChild(host);
      // every text node on its own, so a label and its value never run together
      const text = () => { const w = document.createTreeWalker(host, NodeFilter.SHOW_TEXT);
        const parts = []; while (w.nextNode()) parts.push(w.currentNode.nodeValue); return parts.join(' '); };
      const all = text();
      host.querySelectorAll('details').forEach(d => d.remove());
      const shown = text();
      const q = (s) => [...host.querySelectorAll(s)].map(e => e.textContent.trim());
      host.remove();
      return {all, shown, head: q('.oc-head'), rows: q('.oc-row .oc-txt'),
              tags: q('.oc-tag'), facts: q('.oc-fact dt'), titles: q('.oc-h')};
    })()""".replace("__TEXT__", json.dumps(text)).replace("__CONCERNS__", json.dumps(concerns)))


def _words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z]{4,}", text)}


@pytest.mark.parametrize("text,concerns", [(CONCERNS, True), (PASSED, False), (FAILED, False)],
                         ids=["concerns", "passed", "failed"])
def test_no_word_of_the_message_is_lost(live, text, concerns):
    out = _render(live, text, concerns)
    # the outcome line itself becomes the headline; "(see above)" points at what is now beside it;
    # two fact labels and the "Reviewer note" prefix are said in plain words / by their heading
    dropped = {"generation", "completed", "successfully", "complete", "above",
               "preference", "required", "deliverable", "note"}
    missing = _words(text) - _words(out["all"]) - dropped
    assert not missing, f"words the summary dropped: {sorted(missing)}"


def test_a_delivered_mesh_with_concerns_reads_as_a_headline_rows_and_facts(live):
    out = _render(live, CONCERNS, concerns=True)
    assert out["head"] == ["Your mesh is ready, with points to weigh."]
    assert out["rows"] == ["Cells degrade where the mesh steps from coarse to fine"]
    assert out["tags"] == ["Important"]
    assert out["facts"] == ["Engine", "Detail level", "Download", "Review"]
    # the reviewer's long finding is folded away, not on the summary
    assert "Nevertheless" not in out["shown"] and "Nevertheless" in out["all"]
    assert "71.1" not in out["shown"] and "71.0953" in out["all"]
    assert "  - " not in out["all"] and "IMPORTANT -" not in out["all"]


def test_a_passed_mesh_lists_its_notes_with_numbers_an_engineer_would_print(live):
    out = _render(live, PASSED)
    assert out["head"] == ["Your mesh is ready."]
    assert out["titles"] == ["Reviewer notes"]
    assert out["rows"] == ["Wall layers: 4.71 of 5 layers on average over 97.3% of the wall."]


def test_a_failed_run_keeps_its_reason_and_its_way_forward_on_the_summary(live):
    out = _render(live, FAILED)
    assert out["head"] == ["No mesh this time."]
    assert "Merge them in your CAD" in out["shown"], "the way forward was folded away"
    assert out["titles"] == ["What each attempt did"]
    assert len(out["rows"]) == 2


def test_a_one_line_ending_stays_a_plain_chat_line(live):
    out = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      Stage.mount(); Stage.ensureProc();
      Stage.final({pass:false, attempts:1, text:'it failed'});
      return [...document.querySelectorAll('#cc .im.assistant .txt')].map(t => t.innerHTML);
    })()""")
    assert out == ["it failed"], out
