"""The correction we announce reaches the mesh, or it is not announced.

`ac08697` put the geometry agent's re-reading of a role WE proposed on the last screen before compute:
"I'd proposed o8 as an outlet; looking at the shape it is the end face of a solid part inside the shell, so
I have set it to wall". Nothing carried that into the submitted payload. `engines/port_binding.bind_ports`
binds every boundary condition from `DeclaredPatch.from_intake(p)` - the submitted patches - and
`engines/gmsh/gates` measures the same list; the plan's roles reach neither. So the screen said wall and the
job meshed o8 as an outlet, which is a fact that lies on the one screen where a caveat is still worth
something.

EVERY TEST HERE DRIVES THE SHIPPED `_do_submit_requirements` and reads the payload it stored on the approval
- the same dict `approval.builder_payload` hands the dispatch as `intake_patches` - and, where it matters,
runs the real `port_binding.bind_intake` over it. A test that read a list the executor composed for it would
prove nothing about the boundary the job gets.
"""
from __future__ import annotations

import asyncio

import meshpipeline.agents.intake.admission_token as at
import meshpipeline.agents.intake.engine_selection as es
from meshpipeline.agents.intake import approval as ap
from meshpipeline.agents.intake import executor as ex
from meshpipeline.application import geometry_step as gst
from meshpipeline.application import geometry_survey as gs

_MSGS = [{"role": "user", "content": "internal CFD on this shell, gmsh, you decide everything"}]
_REV = at.revision_of(_MSGS)

#: Three declared ports, each NAMING the mouth it is - which is how the product stopped interrogating a
#: customer about two identical bores, and what `bind_patches` reads before anything is matched by size.
_PATCHES = [{"name": "in", "type": "inlet", "opening_id": "o1", "diameter_mm": 40},
            {"name": "out", "type": "outlet", "opening_id": "o4", "diameter_mm": 60},
            {"name": "out2", "type": "outlet", "opening_id": "o8", "diameter_mm": 60},
            {"name": "shell", "type": "wall"}]
_PARAMS = {"element_order": "2"}
_SUBMIT = {"domain": "a shell with an inner body", "request_txt": "x" * 120,
           "review_brief_txt": "y" * 90, "dimensionality": "3D", "purpose": "internal_cfd",
           "input_kind": "fluid-domain", "mesh_engine": "gmsh", "mesh_fidelity": "standard",
           "engine_source": "user_direct", "engine_params": _PARAMS, "patches": _PATCHES}

#: The measurement, in the fields the binding authorities read: `centroid_mm`/`area_mm2` for
#: `_locate_named_ports`, `centroid_m`/`bore_diameter_m` for `bind_patches`.
_DOC = {"status": "ok", "bbox": {"diagonal_m": 2.0}, "openings": [
    {"id": "o1", "bore_diameter_m": 0.040, "centroid_m": [0.0, 0.0, 0.0],
     "centroid_mm": [0.0, 0.0, 0.0], "area_mm2": 1256.0},
    {"id": "o4", "bore_diameter_m": 0.060, "centroid_m": [1.0, 0.0, 0.0],
     "centroid_mm": [1000.0, 0.0, 0.0], "area_mm2": 2827.0},
    {"id": "o8", "bore_diameter_m": 0.060, "centroid_m": [0.5, 0.4, 0.0],
     "centroid_mm": [500.0, 400.0, 0.0], "area_mm2": 2827.0},
]}

#: The reason off a real rejection on one of these parts, as the agent wrote it.
_WHY = ("patch o8 is the end face of an inner body (a centre body) inside the wall shell; it is an "
        "obstacle face, not a port; the port is the ring's bore around it")


def _answer(mouth: str, role: str, *, ours: bool) -> dict:
    """One stored role answer. The note is read from the module, never restated."""
    return {"question_id": "role_1", "about": "opening.role", "answered_by": gs.CUSTOMER,
            "subject": mouth, "value": role, "option": role, "at": "2026-09-26T00:00:00Z",
            "words": "you decide everything" if ours else f"{mouth} is the {role}",
            "note": gs.ACCEPTED_THE_PROPOSAL if ours else ""}


def _patch(mouth: str, role: str, why: str = "") -> dict:
    """One plan patch, the shape `agent.schema.Patch` dumps."""
    return {"id": mouth, "name": f"{role}_{mouth}", "role": role, "confidence": 0.8,
            "evidence": [why] if why else []}


def _run(answers: list[dict], plan_patches: list[dict] | None, *, patches=None, document=None):
    """The shipped submission. Returns (summary, the payload it stored, the executor state)."""
    sel = es.select_from_structured_input("gmsh", session_id="s", owner_id="u", revision=_REV)
    pats = _PATCHES if patches is None else patches
    canon = at.canonical_payload("gmsh", "internal_cfd", "fluid-domain", "3D", pats, _PARAMS)
    tok = at.issue(session_id="s", owner_id="u", revision=_REV, canonical=canon,
                   verdict="supported", mode=at.SELECTED, selection_id=sel["id"])
    survey: dict | None = None
    if answers or plan_patches is not None:
        survey = {"answers": answers}
        if plan_patches is not None:
            survey["geometry_step"] = {"schema": gst.STEP_SCHEMA, "status": gst.PLANNED, "for": "k1",
                                       "plan": {"patches": plan_patches}}
    st = ex.IntakeExecutionState(owner_id="u", session_id="s", revision=_REV, user_msg_count=1,
                                 selection=sel, pending=tok, geometry_survey=survey,
                                 geometry_document=_DOC if document is None else document)
    exe = ex.IntakeToolExecutor(state=st, job_id="j", implemented_engines=["gmsh"], search_tool=None)
    args = {**_SUBMIT, "patches": [dict(p) for p in pats], "preview_token": tok["token"]}
    out = asyncio.run(exe._do_submit_requirements(args))
    assert out.accepted, out.content
    return str(st.submit_summary), ap.builder_payload(st.approval), st.__dict__


def _roles(payload: dict) -> dict[str, str]:
    return {str(p.get("name")): str(p.get("type")) for p in (payload.get("patches") or [])}


# -------------------------------------------------------------------------------------------------
# a mouth the agent re-reads as solid is not submitted as a port
# -------------------------------------------------------------------------------------------------

def test_a_mouth_we_are_told_is_a_wall_is_not_submitted_as_an_outlet():
    said, payload, _ = _run([_answer("o8", "outlet", ours=True)], [_patch("o8", "wall", _WHY)])
    assert "o8: I proposed outlet, and it is wall" in said, said
    assert "out2" not in _roles(payload), (
        "the screen says o8 is a wall and the payload still declares an outlet on it")
    assert _roles(payload) == {"in": "inlet", "out": "outlet", "shell": "wall"}


def test_the_boundary_the_real_binder_produces_is_the_one_we_announced():
    """The far end of the chain, with the shipped binder and no stand-in for it. An opening no declared port
    claims is folded into the wall surface, which is `bind_ports`'s own word for meshed as wall."""
    from meshpipeline.engines.port_binding import bind_intake

    _said, payload, _ = _run([_answer("o8", "outlet", ours=True)], [_patch("o8", "wall", _WHY)])
    t = {"bbox_min": (0.0, 0.0, 0.0), "bbox_max": (1.2, 0.5, 0.5),
         "stls": {"wall": "w.stl", "p_o1": "a.stl", "p_o4": "b.stl", "p_o8": "c.stl"},
         "openings": {
             "p_o1": {"area": 1256.0e-6, "centroid": (0.0, 0.0, 0.0)},
             "p_o4": {"area": 2827.0e-6, "centroid": (1.0, 0.0, 0.0)},
             "p_o8": {"area": 2827.0e-6, "centroid": (0.5, 0.4, 0.0)}}}
    out, wall, note = bind_intake(t, payload["patches"])
    assert out["binding"]["folded_into_wall"] == ["p_o8"], note
    assert sorted(out["openings"]) == ["in", "out"]
    assert {r["name"]: r["role"] for r in out["binding"]["ports"]} == {"in": "inlet", "out": "outlet"}
    assert wall == "shell"


def test_the_same_declaration_before_the_change_meshed_the_mouth_as_an_outlet():
    """The defect itself, with the binder that was already shipped: this is what the customer got while the
    screen said wall. It fails differently from every assertion above - it never runs the executor."""
    from meshpipeline.engines.port_binding import bind_intake

    t = {"bbox_min": (0.0, 0.0, 0.0), "bbox_max": (1.2, 0.5, 0.5),
         "stls": {"wall": "w.stl", "p_o1": "a.stl", "p_o4": "b.stl", "p_o8": "c.stl"},
         "openings": {
             "p_o1": {"area": 1256.0e-6, "centroid": (0.0, 0.0, 0.0)},
             "p_o4": {"area": 2827.0e-6, "centroid": (1.0, 0.0, 0.0)},
             "p_o8": {"area": 2827.0e-6, "centroid": (0.5, 0.4, 0.0)}}}
    # exactly what `_locate_named_ports` leaves behind: the measured centroid and area, and the model's
    # own diameter dropped so the payload states one size form.
    uncorrected = [{"name": p["name"], "type": p["type"], "opening_id": p["opening_id"],
                    "near_mm": _DOC["openings"][i]["centroid_mm"],
                    "area_mm2": _DOC["openings"][i]["area_mm2"]}
                   for i, p in enumerate(_PATCHES[:3])] + [_PATCHES[3]]
    out, _wall, _note = bind_intake(t, uncorrected)
    assert {r["name"]: r["role"] for r in out["binding"]["ports"]}["out2"] == "outlet"
    assert out["binding"]["folded_into_wall"] == []


def test_a_swapped_role_is_submitted_the_way_round_we_announced():
    said, payload, _ = _run([_answer("o4", "outlet", ours=True)],
                            [_patch("o4", "inlet", "o4 is fed from the header")])
    assert "o4: I proposed outlet, and it is inlet" in said, said
    assert _roles(payload)["out"] == "inlet"
    assert _roles(payload)["in"] == "inlet" and _roles(payload)["out2"] == "outlet"


def test_the_setup_block_the_customer_reads_names_the_corrected_role():
    """The faces line is composed from the payload, so it is the corrected one - the correction block below
    it explains a line that already agrees with the mesh, rather than contradicting one that does not."""
    said, _payload, _ = _run([_answer("o4", "outlet", ours=True)], [_patch("o4", "inlet", "swap")])
    faces = [ln for ln in said.splitlines() if ln.strip().startswith("faces")][0]
    assert "out (inlet on o4)" in faces, faces


# -------------------------------------------------------------------------------------------------
# the approval is fingerprinted over the patches that will run
# -------------------------------------------------------------------------------------------------

def test_the_dispatch_consistency_check_passes_on_the_corrected_payload():
    """`approval.assert_payload_matches_approval` recomputes the builder canonical from the patches it is
    about to dispatch and refuses the run when it does not match the approval's fingerprint. Fingerprinting
    the PREVIEWED patches while dispatching corrected ones turns this feature into "Internal consistency
    error: the approved configuration and the run payload do not match", every time."""
    _said, payload, state = _run([_answer("o8", "outlet", ours=True)], [_patch("o8", "wall", _WHY)])
    snap = state["approval"]
    rebuilt = at.canonical_payload(payload["mesh_engine"], payload["purpose"], payload["input_kind"],
                                   payload["dimensionality"], payload["patches"],
                                   payload["engine_params"])
    assert at.fingerprint(rebuilt) == snap["fingerprint"]
    assert snap["fingerprint"] != state["pending"]["fingerprint"], (
        "the approved fingerprint did not move, so either nothing was corrected or the record is stale")
    assert at.fingerprint(snap["intent_canonical"]) == snap["intent_fingerprint"]


def test_the_approved_port_declaration_carries_the_corrected_set():
    """`approved_intent_canonical`'s v4 `port_declaration` exists so "the binding the engines perform is
    provably the binding that was approved". A withdrawn port must be gone from it too."""
    _said, _payload, state = _run([_answer("o8", "outlet", ours=True)], [_patch("o8", "wall", _WHY)])
    declared = state["approval"]["intent_canonical"].get("port_declaration") or []
    assert "out2" not in [str(p.get("name")) for p in declared], declared


def test_nothing_corrected_leaves_the_approval_on_the_previewed_canonical():
    _said, _payload, state = _run([_answer("o8", "outlet", ours=True)], [_patch("o8", "outlet", _WHY)])
    assert state["approval"]["canonical"] == state["pending"]["canonical"]
    assert state["approval"]["fingerprint"] == state["pending"]["fingerprint"]


# -------------------------------------------------------------------------------------------------
# what cannot be honoured is not said
# -------------------------------------------------------------------------------------------------

def test_a_role_no_payload_can_carry_is_neither_applied_nor_announced():
    """`bind_ports` refuses any role but wall, inlet and outlet ("unknown roles for patches"), so a plan
    that calls a mouth a symmetry plane is a correction this platform cannot make. Announcing it would be
    the exact defect this file is about, one role along."""
    said, payload, state = _run([_answer("o8", "outlet", ours=True)],
                                [_patch("o8", "symmetry", "it is the cut plane")])
    assert ex.ROLES_I_CHANGED_HEAD not in said
    assert _roles(payload)["out2"] == "outlet", "an unannounced role changed the mesh"
    rows = state["geometry_survey"]["geometry_step"]["role_corrections"]["rows"]
    assert [r["action"] for r in rows] == [gst.CORRECTION_WITHDRAWN]
    # THE ROLE FILTER'S OWN WORDS, not the payload gate's. `validate_submission` happens to refuse a
    # symmetry patch on this engine too, so a test that accepted either reason would pass while the filter
    # that must never let one of these through was gone.
    assert "cannot carry the role 'symmetry'" in rows[0]["withheld_because"], rows[0]


def test_dropping_the_last_outlet_is_refused_whole_rather_than_half_applied():
    """The one thing removing a declaration must never do. `validate_submission` is the authority that
    catches it - "patches must include either both 'inlet'+'outlet' or a single 'farfield'" - and it can
    fail differently from everything above: nothing in the correction reads a patch structure."""
    one_out = [_PATCHES[0], _PATCHES[1], _PATCHES[3]]
    said, payload, state = _run([_answer("o4", "outlet", ours=True)],
                                [_patch("o4", "wall", "o4 is solid too")], patches=one_out)
    assert ex.ROLES_I_CHANGED_HEAD not in said
    assert _roles(payload) == {"in": "inlet", "out": "outlet", "shell": "wall"}
    rows = state["geometry_survey"]["geometry_step"]["role_corrections"]["rows"]
    assert [r["action"] for r in rows] == [gst.CORRECTION_WITHDRAWN]
    assert "not be a valid submission" in rows[0]["withheld_because"]


def test_a_flow_role_on_a_mouth_with_no_declared_port_is_not_a_port_we_invent():
    """The plan reads o9 as an inlet and the payload declares no port there. Conjuring one would be the
    platform declaring a boundary on its own authority, which is a larger thing than correcting a role it
    proposed - so nothing is announced, and the row says that rather than something about roles."""
    doc = {**_DOC, "openings": [*_DOC["openings"], {"id": "o9"}]}
    said, payload, state = _run([_answer("o9", "outlet", ours=True)],
                                [_patch("o9", "inlet", "it is fed from the header")], document=doc)
    assert ex.ROLES_I_CHANGED_HEAD not in said
    assert _roles(payload) == {"in": "inlet", "out": "outlet", "out2": "outlet", "shell": "wall"}
    rows = state["geometry_survey"]["geometry_step"]["role_corrections"]["rows"]
    assert [r["action"] for r in rows] == [gst.CORRECTION_WITHDRAWN]
    assert "no declared port binds this mouth" in rows[0]["withheld_because"], rows[0]


def test_a_rewrite_that_matched_no_patch_is_not_announced_as_one_that_landed():
    """The original defect wearing the new function's clothes: the block on screen and the payload
    untouched. `_apply_the_corrections` reads the result back rather than trusting that a patch name taken
    from `bind_patches` must be in the list it came from."""
    sel = es.select_from_structured_input("gmsh", session_id="s", owner_id="u", revision=_REV)
    canon = at.canonical_payload("gmsh", "internal_cfd", "fluid-domain", "3D", _PATCHES, _PARAMS)
    tok = at.issue(session_id="s", owner_id="u", revision=_REV, canonical=canon,
                   verdict="supported", mode=at.SELECTED, selection_id=sel["id"])
    st = ex.IntakeExecutionState(owner_id="u", session_id="s", revision=_REV, user_msg_count=1,
                                 selection=sel, pending=tok, geometry_document=_DOC)
    exe = ex.IntakeToolExecutor(state=st, job_id="j", implemented_engines=["gmsh"], search_tool=None)
    args = {**_SUBMIT, "patches": [dict(p) for p in _PATCHES]}
    records = [{"mouth": "o8", "proposed": "outlet", "planned": "wall", "because": _WHY,
                "patch": "a_patch_no_payload_carries", "action": gst.CORRECTION_UNPORTED}]
    announced, rewrote = exe._apply_the_corrections(args, records)
    assert (announced, rewrote) == ([], False)
    assert args["patches"] == [dict(p) for p in _PATCHES]
    assert records[0]["action"] == gst.CORRECTION_WITHDRAWN
    assert "did not land" in records[0]["withheld_because"], records[0]


def test_a_payload_whose_ports_did_not_all_bind_says_nothing_and_changes_nothing():
    """`bind_patches.checked` is false when any declared port bound to no measured mouth, and then nothing
    here knows what the payload does with a mouth. A partial reading is not a reading."""
    stray = [*_PATCHES[:3], {"name": "extra", "type": "outlet", "diameter_mm": 999},
             _PATCHES[3]]
    said, payload, state = _run([_answer("o8", "outlet", ours=True)],
                                [_patch("o8", "wall", _WHY)], patches=stray)
    assert ex.ROLES_I_CHANGED_HEAD not in said
    assert _roles(payload)["out2"] == "outlet"
    rows = state["geometry_survey"]["geometry_step"]["role_corrections"]["rows"]
    assert [r["action"] for r in rows] == [gst.CORRECTION_WITHDRAWN]


def test_with_no_measurement_nothing_is_announced_and_nothing_is_rewritten():
    said, payload, _ = _run([_answer("o8", "outlet", ours=True)], [_patch("o8", "wall", _WHY)],
                            document={"status": "failed"})
    assert ex.ROLES_I_CHANGED_HEAD not in said
    assert _roles(payload)["out2"] == "outlet"


# -------------------------------------------------------------------------------------------------
# a role the customer TYPED is never rewritten, whatever the plan says
# -------------------------------------------------------------------------------------------------

def test_a_role_the_customer_typed_is_never_rewritten_out_of_the_payload():
    """The case a reviewer builds to break this. `_the_proposal_recorded` is the only writer of the note
    this filters on, so a role they typed cannot reach the rewrite - and the payload proves it did not."""
    said, payload, state = _run([_answer("o8", "outlet", ours=False)], [_patch("o8", "wall", _WHY)])
    assert ex.ROLES_I_CHANGED_HEAD not in said
    assert _roles(payload) == {"in": "inlet", "out": "outlet", "out2": "outlet", "shell": "wall"}
    assert state["approval"]["fingerprint"] == state["pending"]["fingerprint"]
    assert "role_corrections" not in state["geometry_survey"]["geometry_step"]


def test_a_role_they_typed_over_our_proposal_takes_the_rewrite_with_it():
    rows = [{**_answer("o8", "outlet", ours=True), "retired": True},
            _answer("o8", "outlet", ours=False)]
    said, payload, _ = _run(rows, [_patch("o8", "wall", _WHY)])
    assert ex.ROLES_I_CHANGED_HEAD not in said
    assert _roles(payload)["out2"] == "outlet"


# -------------------------------------------------------------------------------------------------
# the row records which of the two things happened
# -------------------------------------------------------------------------------------------------

def test_the_row_says_a_correction_changed_the_mesh():
    _said, _payload, state = _run([_answer("o8", "outlet", ours=True)], [_patch("o8", "wall", _WHY)])
    rec = state["geometry_survey"]["geometry_step"]["role_corrections"]
    assert rec["schema"] == gst.ROLE_CORRECTIONS_SCHEMA and rec["for"] == "k1"
    assert (rec["announced"], rec["rewrote_the_payload"], rec["withdrawn"]) == (1, 1, 0)
    assert rec["rows"] == [{"mouth": "o8", "proposed": "outlet", "planned": "wall",
                            "action": gst.CORRECTION_UNPORTED, "patch": "out2",
                            "because": _WHY, "withheld_because": ""}]


def test_an_announced_correction_and_a_withdrawn_one_are_not_the_same_row():
    """The learning loop's whole question. Both used to be the same absence of a row."""
    _said, _payload, state = _run(
        [_answer("o8", "outlet", ours=True), _answer("o4", "outlet", ours=True)],
        [_patch("o8", "wall", _WHY), _patch("o4", "farfield", "no")])
    rec = state["geometry_survey"]["geometry_step"]["role_corrections"]
    assert (rec["announced"], rec["rewrote_the_payload"], rec["withdrawn"]) == (1, 1, 1)
    assert {r["mouth"]: r["action"] for r in rec["rows"]} == {
        "o8": gst.CORRECTION_UNPORTED, "o4": gst.CORRECTION_WITHDRAWN}


def test_a_correction_that_needed_no_rewrite_is_its_own_outcome():
    """No declared port binds o9, so nothing meshes it as a port and the announcement is already true. It is
    announced and it rewrote nothing, which is a third thing and not either of the other two."""
    doc = {**_DOC, "openings": [*_DOC["openings"], {"id": "o9"}]}
    said, payload, state = _run([_answer("o9", "outlet", ours=True)], [_patch("o9", "wall", "solid")],
                                document=doc)
    assert "o9: I proposed outlet, and it is wall" in said
    assert _roles(payload) == {"in": "inlet", "out": "outlet", "out2": "outlet", "shell": "wall"}
    rec = state["geometry_survey"]["geometry_step"]["role_corrections"]
    assert (rec["announced"], rec["rewrote_the_payload"], rec["withdrawn"]) == (1, 0, 0)
    assert rec["rows"][0]["action"] == gst.CORRECTION_ALREADY


def test_a_failed_plan_drops_the_record_of_what_its_corrections_did():
    """A correction describes a plan. `STALE_ON_A_FAILED_PLAN` is the one list that says which keys go with
    a plan the row no longer carries, and this key is in it for that reason."""
    assert "role_corrections" in gst.STALE_ON_A_FAILED_PLAN


def test_the_row_is_not_written_against_a_step_that_never_planned():
    state = {"geometry_step": {"status": gst.FAILED, "for": "k1", "reason": "no plan"}}
    assert gst.with_role_corrections(state, [{"mouth": "o8", "action": gst.CORRECTION_UNPORTED}]) == state
    assert gst.with_role_corrections({}, [{"mouth": "o8"}]) == {}
