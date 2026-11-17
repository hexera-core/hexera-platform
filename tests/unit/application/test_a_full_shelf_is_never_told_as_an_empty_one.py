# Responsibility: Prove a terminal record that HOLDS a complete delivery never says there is nothing to download, and that a failure holding nothing still reads word for word as it always has.
# Boundaries: the rendered closing message only; no run, no store, no database.
# THE LIVE RECORD (Corintis cold plate, job 0ff6f9e5, 2026-11): 283,410,955 cells meshed in 5h07m,
# executor_success true, no failed gate, the inconclusive review forgiven, and then delivery
# refused by a 10,616,027,998-byte bundle against an int4 artifacts.size_bytes. The mesh was
# delivered afterwards from the same workspace (application/redeliver.py), so the record now
# carries required_ready=true, four delivered types and no missing outputs, while the job row
# stays failed. The message rendered beside the drawn mesh still read "No mesh this time",
# "No downloadable mesh deliverable is available" and "run it again ... nothing needs to change" -
# three claims the user could see were false, because the panel beside them was offering three
# downloads totalling 10.4 GB and drawing 283,410,955 cells.
from __future__ import annotations

import uuid

from meshpipeline.application import redeliver as RD
from meshpipeline.application.final_result import (
    DELIVERED_SENTINEL,
    FailureCategory,
    FinalResult,
    TerminalStatus,
    build_final_result,
    never_started_result,
    render_message,
    worker_lost_result,
)
from meshpipeline.contracts.failure_cause import FailureCause
from meshpipeline.persistence.models import JobStatus

#: the console keys its own headline off this exact line (ui/js/render/outcome.js FAILURE):
#: while the message carries it, the panel prints "No mesh this time." over the drawn mesh
CONSOLE_FAILURE_SENTINEL = "Mesh generation did not complete successfully."

EMPTY_SHELF = "No downloadable mesh deliverable is available."


def _plate(**over) -> FinalResult:
    """Job 0ff6f9e5's terminal record as the database holds it after the re-delivery."""
    d = {
        "schema_version": 5, "job_id": "0ff6f9e5-4f85-49ce-9ca3-a63345199d59",
        "owner_id": "rehaan-dev", "status": "failed", "engine": "cfmesh_conjugate",
        "purpose": "conjugate_heat_transfer", "dimensionality": "3D",
        "effective_mesh_fidelity": "standard", "mesh_fidelity_source": "default",
        "fidelity_policy_version": "3tier-v1", "requested_mesh_fidelity": None,
        "approved_snapshot_id": "", "executor_success": True, "reviewer_verdict": None,
        "review_execution": "failed_to_complete", "failed_gate": "", "patch_contract_ok": True,
        "outcome_code": "internal_pipeline_failure",
        "failure_category": "internal_pipeline_failure", "attempts": 1, "attempts_max": 5,
        "required_ready": True,
        "delivered_types": ["mesh", "mesh_bundle", "repair_report", "viewer_data"],
        "missing_outputs": [], "requirement_caveats": [], "optional_warnings": [],
        "failure_cause": "review_incomplete",
        "failure_detail": ("Your mesh was built and passed every automatic check, but our final "
                           "review of it did not finish: the review did not have everything it "
                           "needs to judge the mesh. That is on our side, not a problem with "
                           "your geometry or your request."),
        "failure_next_step": ("You can run it again (say \"run it again\" in this chat) with the "
                              "same request - nothing needs to change."),
        "finalized_at": "2026-11-16T21:56:37.472388+00:00",
    }
    d.update(over)
    return FinalResult.from_dict(d)


def _empty(**kw) -> FinalResult:
    """An ordinary failure with nothing on the shelf: required_ready False, nothing delivered."""
    base = {"job_id": "j", "owner_id": "o", "status": TerminalStatus.failed, "engine": "snappy",
            "purpose": "external_cfd", "dimensionality": "3D", "approved_snapshot_id": "s",
            "executor_success": False, "reviewer_verdict": "", "failed_gate": "",
            "api_failure": "", "attempts": 1, "attempts_max": 4, "required_ready": False,
            "delivered_types": [], "optional_warnings": []}
    base.update(kw)
    return build_final_result(**base)


def test_the_plate_on_the_shelf_is_not_told_as_an_empty_one():
    """Every claim the user could see was false is gone, and what is on the shelf is said."""
    msg = render_message(_plate())
    assert EMPTY_SHELF not in msg
    assert "nothing needs to change" not in msg
    # the console's "No mesh this time." comes from this line being present at all
    assert CONSOLE_FAILURE_SENTINEL not in msg
    assert "ready to download" in msg
    assert "Nothing needs to be run again to get this mesh" in msg
    # what failed is still said, in the record's own recorded words
    assert "our final review of it did not finish" in msg


def test_the_run_is_still_told_as_failed():
    """The shelf changed, the run did not. The message says both, and re-decides nothing."""
    fr = _plate()
    msg = render_message(fr)
    assert "still recorded as failed" in msg
    assert "Mesh generation completed successfully." not in msg
    assert fr.status is TerminalStatus.failed
    assert fr.failure_category == "internal_pipeline_failure"
    assert fr.failure_cause == "review_incomplete"


def test_a_mesh_nobody_judged_is_never_said_to_have_passed():
    """The reviewer was REACHED and could not finish, so there is no verdict in either
    direction - and a downloadable mesh must not read as a reviewed one."""
    msg = render_message(_plate())
    assert "Review: did not finish, so this mesh has no verdict" in msg
    assert "Review: passed" not in msg
    assert "did not pass" not in msg


def test_a_handover_failure_that_was_made_good_stops_saying_nothing_was_delivered():
    """The delivery account ends 'so nothing was delivered' and sends the user nowhere but back
    to the same store call. Both are refuted once the files are stored."""
    fr = _plate(failure_category="delivery_failed", outcome_code="delivery_failed",
                failure_cause=FailureCause.DELIVERY_FAILED.value,
                failure_detail=("Your mesh was built and passed every automatic check, but it "
                                "could not be stored for download, so nothing was delivered. "
                                "This is our fault, not your geometry's or your request's."),
                failure_next_step=("Running it again rebuilds the whole mesh and ends at the "
                                   "same storing step, which has just failed on our side."))
    msg = render_message(fr)
    assert "nothing was delivered" not in msg
    assert "ends at the same storing step" not in msg
    assert "handover to the store failed afterwards" in msg
    assert "ready to download" in msg


def test_a_record_short_of_a_required_class_is_not_a_full_shelf():
    """required_ready and the delivered list are written by different steps. A record that says
    ready while a required class is missing must keep reading as an empty shelf: everything the
    full-shelf wording unlocks is a claim that the user can download the mesh."""
    short = render_message(_plate(delivered_types=["mesh", "repair_report", "viewer_data"]))
    assert EMPTY_SHELF in short                      # mesh_bundle is required and absent
    still_missing = render_message(_plate(missing_outputs=["mesh_bundle"]))
    assert EMPTY_SHELF in still_missing
    unready = render_message(_plate(required_ready=False))
    assert EMPTY_SHELF in unready


# The three wordings a failure with nothing delivered can end on, pinned whole. This is the
# common case and it must not move by one character: the full-shelf branch is asked before any
# of these lines is appended, so none of them can be reached by a record that holds a delivery.
GATE = ('Mesh generation did not complete successfully.\n'
        'The mesh was built, but its cells are too badly shaped to trust: 0.2% of the faces are '
        'badly skewed, and at most 0.05% may be.\n'
        'No downloadable mesh deliverable is available.\n'
        'You can run it again (say "run it again" in this chat), or tell me what to change '
        'first - for example a finer mesh near sharp edges, or fewer boundary layers.')

EXHAUSTED = ('Mesh generation did not complete successfully.\n'
             'A valid mesh could not be produced within the allowed attempts.\n'
             'No downloadable mesh deliverable is available.\n'
             'Attempts used: 4/4.\n'
             'You can run it again from this chat: say "run it again", or tell me what to '
             'change first.')

WORKER_LOST = ('Mesh generation did not complete successfully.\n'
               'No downloadable mesh deliverable is available.\n'
               'The worker running this job was lost before it finished, so the run was marked '
               'failed. This is on our side, not your geometry or request - please try again: '
               'say "run it again" in this chat to start a new run.')


def test_a_failure_with_an_empty_shelf_reads_exactly_as_it_always_has():
    gate = _empty(failed_gate="quality_floor", failure_cause=FailureCause.MESH_QUALITY,
                  failure_facts={"checks": [{"key": "skew_fraction",
                                             "label": "Skewed faces are localized",
                                             "measured": 0.002, "op": "<=",
                                             "threshold": 0.0005}]})
    assert render_message(gate) == GATE                       # a gate that named its cause
    assert render_message(_empty(attempts=4)) == EXHAUSTED    # the category headline alone
    assert render_message(worker_lost_result(job_id="j", owner_id="o", attempts=1,
                                             attempts_max=4)) == WORKER_LOST  # the class message


# THE SENTINEL (defect 1). The console reads ONE line of this message to pick its headline and
# its wrapper class: ui/js/render/outcome.js matches "Mesh generation completed successfully."
# as kind "done", "Mesh generation did not complete successfully." as kind "failed", and this
# third line as kind "delivered". A message that carries none of the three parses to kind "" -
# HEADLINE[""] is undefined, the fallback is the empty string, and the panel prints no headline
# at all and loses the oc-<kind> class ui/css/theme.css styles. That is what this message did
# while it emitted no sentinel of its own.
SENTINEL = "Mesh generation completed; the run did not."


def test_the_full_shelf_message_opens_with_the_line_the_console_keys_on():
    """FIRST line, exact text. outcome.js matches the whole line, so a comma moved in either
    place costs the panel its headline silently - nothing throws, the heading is just gone."""
    assert DELIVERED_SENTINEL == SENTINEL
    msg = render_message(_plate())
    assert msg.split("\n")[0] == SENTINEL, f"the first line is {msg.split(chr(10))[0]!r}"
    # and it is a sentence that is TRUE if the parser ever fails to strip it: the mesh
    # generation did complete, and the run did not
    assert "successfully" not in SENTINEL
    # the two older sentinels stay out of it: either one would be read as the wrong kind
    assert CONSOLE_FAILURE_SENTINEL not in msg
    assert "Mesh generation completed successfully." not in msg


# ---------------------------------------------------------------------------------------------
# NO CATEGORY CONTRADICTS THE SHELF (defect 2)

#: the re-run instructions that can arrive INSIDE a category's own sentence, where the message's
#: own next step one line below cannot take them back
RERUN_WORDINGS = ('say "run it again"', "please try again", "upload it again", "run it again")
#: and the claims that deny the mesh the same panel is serving
DENIALS = ("A valid mesh could not be produced",
           "This geometry cannot be meshed as it is",   # the words failure_cause.py emits,
           "The geometry cannot be meshed as it is",    # and the words the category table did
           "The mesh could not be generated",
           "No downloadable mesh deliverable is available")


def _shelf_of_every_category():
    for cat in FailureCategory:
        # no recorded account: the fallback path, which is where the category's own prose used
        # to be borrowed from
        yield cat, render_message(_plate(failure_category=cat.value, outcome_code=cat.value,
                                         failure_detail="", failure_cause=""))


def test_no_category_tells_the_user_to_run_it_again_on_a_full_shelf():
    """worker_lost and never_started carried their re-run instruction INSIDE the class message
    errors.py owns, so the message read 'please try again: say "run it again"' one line above
    'Nothing needs to be run again to get this mesh'. All thirteen are checked, not those two:
    the branch is chosen by the shelf and never by the category, so any of them can arrive."""
    for cat, msg in _shelf_of_every_category():
        assert "Nothing needs to be run again to get this mesh" in msg
        for bad in RERUN_WORDINGS:
            assert bad not in msg, f"{cat.value} still tells the user to re-run: {msg!r}"


def test_the_screened_set_and_the_prose_cannot_drift_apart():
    """The set in failure_cause is a claim ABOUT THE SENTENCES, so it is checked against them.

    Screening by name would rot the moment someone reworded a cause or added a seventeenth. This
    reads every cause's own text and asserts the set is exactly the ones that deny a mesh, so a new
    denial fails here rather than appearing under a download button.
    """
    from meshpipeline.contracts.failure_cause import (
        ACCOUNTS_DENY_A_MESH,
        FailureCause,
        describe,
    )

    # DELIVERY_FAILED says "nothing was delivered" and is deliberately not in the set: the renderer
    # tells that one again in its own words rather than dropping it, so it is excused by name here
    # and nowhere else.
    told_again = {FailureCause.DELIVERY_FAILED}
    denies = set()
    for cause in FailureCause:
        what = (describe(cause, {})[0] or "").lower()
        if any(w in what for w in ("no mesh was built", "nothing was delivered",
                                   "nothing was built", "upload it again", "fix the geometry")):
            denies.add(cause)
    assert denies - told_again == set(ACCOUNTS_DENY_A_MESH), (
        "failure_cause.ACCOUNTS_DENY_A_MESH no longer matches the sentences it describes. "
        f"the prose denies a mesh for {sorted(c.value for c in denies - told_again)}, "
        f"the set holds {sorted(c.value for c in ACCOUNTS_DENY_A_MESH)}")


def _shelf_of_every_recorded_cause():
    """Every cause as it actually arrives: as fr.failure_detail, which is describe(cause)[0].

    The category sweep above passes failure_detail="" and so only ever walks the fallback branch.
    gate_failed, review_rejected and input_rejected records ALWAYS carry a detail, so that branch
    is the one a real full shelf takes, and it was unscreened.
    """
    from meshpipeline.contracts.failure_cause import FailureCause, describe
    for cause in FailureCause:
        what = describe(cause, {})[0]
        if not what:
            continue
        yield cause, render_message(_plate(failure_category="gate_failed",
                                           outcome_code="gate_failed",
                                           failure_detail=what, failure_cause=cause.value))


def test_no_recorded_cause_denies_the_mesh_that_is_on_the_shelf():
    """The hole the category sweep could not see. FailureCause.NOT_BUILT put "No mesh was built:"
    one line under "the deliverables on this result are ready to download", and
    FailureCause.GEOMETRY_REJECTED put "Fix the geometry and upload it again." one line above
    "Nothing needs to be run again to get this mesh". Both arrive through failure_detail, which
    the first version of this branch handed to the user verbatim."""
    for cause, msg in _shelf_of_every_recorded_cause():
        assert "ready to download" in msg
        for bad in DENIALS + RERUN_WORDINGS:
            assert bad not in msg, f"{cause.value} still contradicts the shelf: {msg!r}"


def test_a_screened_cause_is_dropped_and_never_reworded():
    """Rewriting a recorded account so it agrees with the shelf would be inventing a cause. The
    account is dropped and the classification is said flatly instead; the cause itself stays in
    the record for anyone reading it."""
    from meshpipeline.contracts.failure_cause import FailureCause, describe

    what = describe(FailureCause.NOT_BUILT, {})[0]
    msg = render_message(_plate(failure_category="gate_failed", outcome_code="gate_failed",
                                failure_detail=what, failure_cause=FailureCause.NOT_BUILT.value))
    assert "No mesh was built" not in msg
    assert 'classifying it as "gate failed"' in msg
    # and the forward pointer is not made, because no account is being pointed at
    assert "and this is what failed" not in msg


def test_no_category_denies_the_mesh_that_is_on_the_shelf():
    """attempts_exhausted emitted 'A valid mesh could not be produced within the allowed
    attempts.' one line below 'the deliverables on this result are ready to download'."""
    for cat, msg in _shelf_of_every_category():
        assert "ready to download" in msg
        for bad in DENIALS:
            assert bad not in msg, f"{cat.value} denies the stored mesh: {msg!r}"


def test_the_facts_a_category_does_own_are_still_said():
    """Dropping the category's PROSE is not dropping its facts: the attempt count is a measured
    number and it still rides with the message."""
    msg = render_message(_plate(failure_category="attempts_exhausted",
                                outcome_code="attempts_exhausted",
                                failure_detail="", failure_cause="", attempts=4, attempts_max=4))
    assert "Attempts used: 4/4." in msg
    assert "The record does not say what failed beyond classifying it as" in msg


# REACHABILITY, checked rather than believed. The docstring of _failed_but_delivered says which
# categories can reach it; these two pin the one fact that rules a category out, so a change
# that makes worker_lost reachable breaks a test instead of a user's message.

class _Log:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


class _Session:
    def __init__(self, row=None): self.row = row
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def commit(self): return None


class _Repo:
    def __init__(self, row): self.row = row
    async def get_internal(self, db, jid): return self.row
    async def get_final_result(self, db, jid): return self.row.final_result
    async def set_final_result(self, db, jid, record): self.row.final_result = record


class _Row:
    def __init__(self, record, status=JobStatus.failed):
        self.id = self.owner_id = "o"
        self.status = status
        self.execution_generation = 1
        self.workspace_purged = False
        self.final_result = record


async def _redeliver(record, *, status=JobStatus.failed, workspace):
    job = str(uuid.uuid4())
    return await RD.redeliver_job(lambda: _Session(), _Repo(_Row(record, status)), job,
                                  jlog=_Log(), workspace=str(workspace))


async def test_a_reaped_record_can_never_reach_the_full_shelf_branch(tmp_path):
    """worker_lost and never_started are the reaper's own, and _reaped_result never sets an
    engine. redeliver_job - the only writer that turns required_ready on over a failed record -
    refuses an engine-less record before it looks at a workspace, so neither category can arrive
    on a full shelf. Checked here because the docstring that says so is load-bearing."""
    ws = tmp_path / "job" / "generation_1" / "attempt_1"
    ws.mkdir(parents=True)
    for maker in (worker_lost_result, never_started_result):
        rec = maker(job_id="j", owner_id="o", attempts=1, attempts_max=4)
        assert rec.engine == "", "a reaped record now names an engine - re-read the docstring"
        out = await _redeliver(rec.to_dict(), workspace=ws)
        assert out.refused == RD.NO_TERMINAL_RECORD
        assert not out.required_ready


# ---------------------------------------------------------------------------------------------
# THE PARK DOES NOT BURY THE SHELF (defect 3)

async def test_a_parked_job_row_is_the_reason_a_park_cannot_hold_a_delivery(tmp_path):
    """The believed reason was that a parked run has no mesh manifest, so redeliver_job refuses
    it with NO_WORKSPACE. That is not the binding one, and it would not survive an operator
    passing workspace= by hand. The binding one is the job ROW: review_parked is stamped only
    where the assembly status is pending_review, and redeliver_job refuses any status that is
    not exactly failed."""
    ws = tmp_path / "job" / "generation_1" / "attempt_1"
    ws.mkdir(parents=True)
    out = await _redeliver(_plate().to_dict(), status=JobStatus.pending_review, workspace=ws)
    assert out.refused == RD.NOT_TERMINAL_FAILED


def test_a_parked_record_holding_a_full_shelf_says_both_things():
    """render_message returned the parked message and nothing else, BEFORE the full-shelf check
    was ever reached, so a record with a stored mesh went back to never mentioning it. Both
    facts are the user's: the mesh is downloadable now, and a decision is still waiting."""
    msg = render_message(_plate(review_parked=2))
    assert "ready to download" in msg
    assert msg.split("\n")[0] == SENTINEL
    assert "Nothing needs to be run again to get this mesh" in msg
    # and the park is not lost on the way
    assert "The repair is waiting on you" in msg
    assert "It left 2 things for you to decide" in msg


def test_an_empty_shelf_that_is_parked_still_reads_as_the_park_alone():
    """The common case does not move: a parked failure with nothing delivered is the parked
    message, by itself, exactly as before."""
    parked = _plate(review_parked=2, required_ready=False, delivered_types=[],
                    missing_outputs=["mesh", "mesh_bundle"])
    msg = render_message(parked)
    assert msg == ("The repair is waiting on you. It left 2 things for you to decide, and "
                   "nothing was changed without you: your file is as it was except for the "
                   "repairs it was sure of. Review them and the run will pick up from there.")


# ---------------------------------------------------------------------------------------------
# THE COMMENT THAT SHIPS SAYS WHAT THE CODE DOES (defect 4)

def test_the_renderer_reads_required_ready_in_the_failure_half():
    """The substance behind the comment: flipping required_ready alone, on a record whose status
    stays failed, changes the closing message. The re-delivery's own docstring used to say the
    renderer reads that field only inside its succeeded branch."""
    full, empty = _plate(), _plate(required_ready=False)
    assert full.status is TerminalStatus.failed and empty.status is TerminalStatus.failed
    assert render_message(full) != render_message(empty)
    assert EMPTY_SHELF in render_message(empty)
    assert EMPTY_SHELF not in render_message(full)


def test_the_re_delivery_docstring_no_longer_says_the_opposite_of_the_code():
    """A comment stating the opposite of the code is a defect that ships, and this file is the
    one that catches it: the claim was specific enough to pin."""
    doc = RD._restate_delivery.__doc__ or ""
    assert "only inside its `succeeded` branch" not in doc
    assert "renders the same sentence before and after" not in doc
    assert "FAILURE half" in doc, "the docstring does not say where the field is now read"


# ---------------------------------------------------------------------------------------------
# NO CAUSE IS PROMISED THAT THE NEXT LINE CANNOT KEEP (defect 5)

def test_the_opening_promises_a_cause_only_when_the_record_recorded_one():
    """'...and this is what failed' is a forward pointer. Job 0ff6f9e5 has a recorded account to
    point at; a record without one used to point at the CATEGORY headline, so the message read
    'and this is what failed' / 'Something went wrong on our side while running the job.' - a
    classification standing in for a cause."""
    with_cause = render_message(_plate())
    assert "and this is what failed." in with_cause
    assert "our final review of it did not finish" in with_cause

    without = render_message(_plate(failure_detail="", failure_cause=""))
    assert "and this is what failed" not in without
    assert "The run is still recorded as failed." in without
    assert ("The record does not say what failed beyond classifying it as "
            '"internal pipeline failure".') in without
    # the category headline is not borrowed as prose either way
    assert "Something went wrong on our side while running the job." not in without


def test_the_handover_keeps_its_promise_of_a_cause():
    """The DELIVERY_FAILED carve-out writes its own account, so the pointer still lands."""
    msg = render_message(_plate(failure_cause=FailureCause.DELIVERY_FAILED.value,
                                failure_detail=""))
    assert "and this is what failed." in msg
    assert "handover to the store failed afterwards" in msg


# ---------------------------------------------------------------------------------------------
# THE LADDER OFFER DOES NOT FIGHT THE NEW NEXT STEP (defect 6)

#: what pipeline/engine_fallback.offer builds for a run that ended with nothing: it opens by
#: denying the mesh, which beside a download button is not a clashing next step but a denial of
#: what is on the screen
OFFER = {"kind": "engine", "engine": "snappy_multiregion", "same_contract": True,
         "reply": "use snappy_multiregion",
         "text": ("cfMesh could not mesh this shape: the mesh failed its quality floor. "
                  "snappy_multiregion can build the same mesh - same boundaries, same units, "
                  'same settings. Reply "use snappy_multiregion" and I will set that run up.')}


def _with_ladder(fr, offer):
    from meshpipeline.application.final_result import with_engine_ladder
    return with_engine_ladder(fr, {"attempts": [], "switches": [], "delivered_by": "",
                                   "offer": offer})


def test_the_engine_offer_is_withheld_from_a_full_shelf():
    """It is empty on 0ff6f9e5 (one attempt, offer null), so this was latent. Suppressed rather
    than reworded: the offer's own first clause denies the stored mesh, and that sentence is
    true of the run it was written for."""
    msg = render_message(_with_ladder(_plate(), OFFER))
    assert "could not mesh this shape" not in msg
    assert "use snappy_multiregion" not in msg
    # the door to another engine is still open, in the message's own words
    assert ("If you want a different mesh, tell me what to change in this chat and I will set "
            "up a new run.") in msg


def test_the_engine_offer_still_rides_with_a_failure_that_delivered_nothing():
    """The suppression is scoped to the shelf. A real failure keeps its offer, word for word."""
    msg = render_message(_with_ladder(_plate(required_ready=False, delivered_types=[],
                                             missing_outputs=["mesh_bundle"]), OFFER))
    assert msg.endswith(OFFER["text"])
    assert EMPTY_SHELF in msg
