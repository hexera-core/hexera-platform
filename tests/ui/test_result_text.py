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

#: A RUN THAT FAILED AND HANDED OVER THE MESH ANYWAY (final_result._failed_but_delivered).
#: Job 0ff6f9e5: 283,410,955 cells, every executor gate passed, the 10.6 GB bundle refused by a
#: 32-bit column, the same workspace delivered afterwards without re-running anything. The job row
#: is still "failed" on purpose - the run did fail - so the message has to carry both halves, and
#: the panel it lands in is offering the downloads and drawing the mesh while it is read.
DELIVERED_ON_A_FAILED_RUN = """Mesh generation completed; the run did not.
Your mesh was built and it is stored: the deliverables on this result are ready to download. The run is still recorded as failed, and this is what failed.
Your mesh was built and every automatic check on it passed, and the handover to the store failed afterwards. That is why the run is recorded as failed.
Nothing needs to be run again to get this mesh: it is already built and stored, and running the same request again would build the same mesh a second time.
If you want a different mesh, tell me what to change in this chat and I will set up a new run.
Required deliverable: ready to download
Review: did not finish, so this mesh has no verdict"""

#: THE SAME ENDING WITH A LONG RECORDED CAUSE. The cause line below is 318 characters, well past
#: outcome.js's SHORT = 170, so it is the only shape in this file that can tell whether the
#: `delivered` kind is shown WHOLE or cut to its first sentence with the rest behind a Details
#: fold. Every other delivered line is under 170, which is why the first version of this fixture
#: could not catch the flag being wrong: reverting it passed all 34 files in this lane.
DELIVERED_WITH_A_LONG_CAUSE = """Mesh generation completed; the run did not.
Your mesh was built and it is stored: the deliverables on this result are ready to download. The run is still recorded as failed, and this is what failed.
A boundary you approved did not come out of the mesher under its approved name and type: the inlet you named came back as two separate patches, one of them with no faces on it at all, and the solver reads that as a wall rather than an opening, so the run was stopped before the result could be handed over. This is our mistake, not your geometry's.
Nothing needs to be run again to get this mesh: it is already built and stored, and running the same request again would build the same mesh a second time.
Required deliverable: ready to download
Review: did not finish, so this mesh has no verdict"""

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
              tags: q('.oc-tag'), facts: q('.oc-fact dt'), titles: q('.oc-h'),
              paras: q('.oc-p'), cls: host.firstElementChild.className};
    })()""".replace("__TEXT__", json.dumps(text)).replace("__CONCERNS__", json.dumps(concerns)))


def _words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z]{4,}", text)}


@pytest.mark.parametrize("text,concerns", [(CONCERNS, True), (PASSED, False), (FAILED, False),
                                           (DELIVERED_ON_A_FAILED_RUN, False)],
                         ids=["concerns", "passed", "failed", "delivered"])
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


def test_a_delivered_mesh_on_a_failed_run_is_an_outcome_of_its_own(live):
    """The headline is the whole reason this kind exists.

    With no sentinel the parser leaves `kind` empty, so the panel printed no headline at all and
    the wrapper carried no oc-KIND class for theme.css to style. Printing "No mesh this time."
    instead would be worse: it is read over a mesh the same page is drawing.
    """
    out = _render(live, DELIVERED_ON_A_FAILED_RUN)
    assert out["head"] == ["Your mesh is ready, after a failure on our side."]
    assert out["cls"] == "oc oc-delivered"
    # the sentinel line is consumed into the headline, as the other two are
    assert "Mesh generation" not in out["all"]


def test_the_delivered_sentinel_is_read_with_or_without_its_period(live):
    """The other two sentinels tolerate a missing trailing period; this one has to match them,
    because a message assembled by a different path can arrive either way."""
    kinds = live.evaluate("""(async () => {
      const { parseOutcome } = await import('/static/js/render/outcome.js');
      return ['Mesh generation completed; the run did not.',
              'Mesh generation completed; the run did not'].map(s => parseOutcome(s).kind);
    })()""")
    assert kinds == ["delivered", "delivered"]


def test_a_delivered_mesh_on_a_failed_run_keeps_the_cause_on_the_summary(live):
    """Why the run failed, and why not to re-run it, are the point of this message - so they are
    shown entire, the way a failed run's paragraphs are, never cut to a first sentence."""
    out = _render(live, DELIVERED_ON_A_FAILED_RUN)
    assert "the handover to the store failed afterwards" in out["shown"]
    assert "running the same request again would build the same mesh" in out["shown"]
    assert out["paras"] == DELIVERED_ON_A_FAILED_RUN.split("\n")[1:5], out["paras"]
    # the two closing lines are facts, not paragraphs, and "Required deliverable" is said plainly.
    # The review line ends in a word, not a colon, so it is not taken for a list title either
    assert out["facts"] == ["Download", "Review"]
    assert out["titles"] == []


def test_a_delivered_run_reads_its_paragraphs_at_full_strength(live):
    """theme.css has to learn the kind as well. Without a rule, oc-delivered falls back to the
    muted grey the standing wording around a successful run's facts is printed in - and these
    paragraphs are the recorded cause of a failure, which a failed run prints at full strength."""
    out = live.evaluate("""(() => {
      const colour = (kind) => {
        const d = document.createElement('div');
        d.className = 'oc' + (kind ? ' oc-' + kind : '');
        d.innerHTML = '<p class="oc-p">x</p>';
        document.body.appendChild(d);
        const c = getComputedStyle(d.firstElementChild).color;
        d.remove();
        return c;
      };
      return {plain: colour(''), failed: colour('failed'), delivered: colour('delivered')};
    })()""")
    assert out["delivered"] == out["failed"], out
    assert out["delivered"] != out["plain"], out


def test_a_one_line_ending_stays_a_plain_chat_line(live):
    out = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      Stage.mount(); Stage.ensureProc();
      Stage.final({pass:false, attempts:1, text:'it failed'});
      return [...document.querySelectorAll('#cc .im.assistant .txt')].map(t => t.innerHTML);
    })()""")
    assert out == ["it failed"], out


def test_a_delivered_meshs_reason_is_shown_whole_and_never_folded_away(live):
    """The `whole` flag at outcome.js, pinned.

    A failed-but-delivered message's paragraphs are the ONLY place that explains why a mesh the
    panel is serving sits on a run recorded as failed. Cut to its first sentence with the rest
    behind a Details fold, the explanation is gone from the screen exactly where it is needed.
    This case has a 318-character cause so the flag actually binds: with
    `const whole = o.kind === "failed"` restored, this test fails and nothing else in the lane
    notices.
    """
    out = _render(live, DELIVERED_WITH_A_LONG_CAUSE)
    tail = "This is our mistake, not your geometry's."
    assert tail in out["shown"], (
        "the recorded cause was cut short on the summary; a delivered mesh's only explanation of "
        "why its run failed must not sit behind a Details fold")
    # and it is one paragraph, not a stub plus a fold
    assert any(tail in p for p in out["paras"])


def test_the_console_and_the_backend_agree_on_the_sentinel_word_for_word(live):
    """The two halves of this feature are joined by one sentence, held in two languages.

    final_result.DELIVERED_SENTINEL is emitted by Python; outcome.js matches it with a regex. Each
    side has its own test pinning it to itself, so rewording the backend constant leaves this whole
    lane green while production silently loses its headline and its wrapper class - which is the
    defect the sentinel exists to cure. This is the only test that reads both, in the same shape
    conftest's `event_vocabulary` uses to tie JS event names to meshpipeline.events.
    """
    from meshpipeline.application.final_result import DELIVERED_SENTINEL

    got = live.evaluate("""(async () => {
      const { parseOutcome, outcomeHtml } = await import('/static/js/render/outcome.js');
      const host = document.createElement('div');
      host.innerHTML = outcomeHtml(__TEXT__, {});
      document.body.appendChild(host);
      const head = [...host.querySelectorAll('.oc-head')].map(e => e.textContent.trim());
      const cls = host.firstElementChild.className;
      host.remove();
      return {kind: parseOutcome(__TEXT__).kind, head, cls};
    })()""".replace("__TEXT__", json.dumps(DELIVERED_SENTINEL)))

    assert got["kind"] == "delivered", (
        f"outcome.js does not recognise final_result.DELIVERED_SENTINEL ({DELIVERED_SENTINEL!r}); "
        "the two halves have been reworded apart and the panel has lost its headline")
    assert got["head"] == ["Your mesh is ready, after a failure on our side."]
    assert "oc-delivered" in got["cls"]
