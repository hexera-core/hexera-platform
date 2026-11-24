# Responsibility: Verify the part's own outside is delivered under the name the user approved for
# it, and that the engine's own cuts are no part of the contract the user agreed to.
# Boundaries: the naming seam and the contract gate. What a mouth IS belongs to mouths.py.
#
# THE RUN THIS FILE EXISTS FOR. Job 2b72862e meshed the first customer cold plate to 279,281,786
# cells, rc 0, deterministic, 0 bad faces, and the product then refused to deliver it at its own
# patch_contract gate. Intake had approved exactly one boundary - `wall -> type wall`, from
# port_declaration.json [{"name": "wall", "type": "wall"}] - and the mesh carried
# {"mouth_2": "patch", "mouth_3": "patch", "wall_outer": "wall"}, with
# quality.port_patches_planned ["mouth_1", "mouth_2", "mouth_3"] and port_patches_dropped
# ["mouth_1"]. Both halves of that mismatch were the engine's own doing: `wall_outer` is the
# constant it names the shell after, and the mouths are holes it found and cut itself. 91 minutes
# of meshing, refused over a name.
from __future__ import annotations

import pytest

from meshpipeline.engines.cfmesh_conjugate import gates as G
from meshpipeline.engines.cfmesh_conjugate import mouths as MO
from meshpipeline.engines.cfmesh_conjugate.conjugate import (
    FLUID_REGION,
    OUTER_PATCH,
    ConjugateError,
    is_domain_face,
    render_port_patch_dicts,
)
from meshpipeline.engines.cfmesh_conjugate.driver import _outer_patch_name

# The real declaration from the refused job, byte for byte.
DECLARED_WALL = [{"name": "wall", "type": "wall"}]


def _ws(tmp_path, declaration):
    import json

    (tmp_path / "port_declaration.json").write_text(json.dumps(declaration))
    return tmp_path


# ---- the name the outside is given ----------------------------------------------------------

def test_one_declared_wall_becomes_the_name_the_outside_is_delivered_under(tmp_path):
    """The defect, in one line. This is the declaration job 2b72862e actually carried."""
    assert _outer_patch_name(_ws(tmp_path, DECLARED_WALL)) == "wall"


def test_no_declared_wall_leaves_the_engines_own_name(tmp_path):
    """Every run before this change, and it must be untouched: a job that named only ports gets
    the shell the engine always gave it."""
    decl = [{"name": "coolant_in", "type": "inlet"}, {"name": "coolant_out", "type": "outlet"}]
    assert _outer_patch_name(_ws(tmp_path, decl)) == OUTER_PATCH


def test_two_declared_walls_leave_the_engines_own_name(tmp_path):
    """On the shell path the part's exterior is ONE surface and arrives as ONE patch, so two
    declared walls cannot both be it. Picking one would deliver a mesh missing the other - which
    is the failure this whole file is about, not a softer version of it."""
    decl = [{"name": "top", "type": "wall"}, {"name": "sides", "type": "wall"}]
    assert _outer_patch_name(_ws(tmp_path, decl)) == OUTER_PATCH


def test_an_empty_workspace_leaves_the_engines_own_name(tmp_path):
    """No port_declaration.json at all - a devtools run, or a job intake never touched."""
    assert _outer_patch_name(tmp_path) == OUTER_PATCH


@pytest.mark.parametrize("bad", ["wall outer", "wall/outer", "", "  "])
def test_a_name_openfoam_cannot_spell_leaves_the_engines_own_name(tmp_path, bad):
    assert _outer_patch_name(_ws(tmp_path, [{"name": bad, "type": "wall"}])) == OUTER_PATCH


def test_an_interface_shaped_name_leaves_the_engines_own_name(tmp_path):
    """finalize.delivered_boundary_types DELETES any name carrying `_to_` - that is how the
    split's own coupling patches are kept out of the user's contract. A declared name shaped like
    one would be dropped there and then reported missing after the mesh: the same hours lost, for
    a different reason."""
    decl = [{"name": "plate_to_ambient", "type": "wall"}]
    assert _outer_patch_name(_ws(tmp_path, decl)) == OUTER_PATCH


def test_a_regions_own_name_leaves_the_engines_own_name(tmp_path):
    assert _outer_patch_name(_ws(tmp_path, [{"name": FLUID_REGION, "type": "wall"}])) \
        == OUTER_PATCH


# ---- and the cut agrees with it --------------------------------------------------------------

def test_a_shell_stub_is_cut_out_of_the_name_the_surface_was_written_under():
    """`port_patches[].face` and the STL solid name are ONE decision. The cut dict selects faces
    with `patchToFace; patch <face>`, and createPatch REMOVES a patch whose set came out empty
    rather than leaving it at zero faces - so a stub still saying `wall_outer` on a run whose
    surface was written as `wall` would make every mouth vanish from a mesh reporting success."""
    ports = [{"centroid_m": (0.0, 0.0, 0.0), "normal": (0.0, 0.0, 1.0),
              "diameter_m": 0.0118, "passage": 1}]
    assert [s.face for s in MO.shell_stubs(ports, None, outer="wall")] == ["wall"]
    assert [s.face for s in MO.shell_stubs(ports, None)] == [OUTER_PATCH]


def test_the_cut_dict_selects_this_runs_own_outer_patch():
    rows = [{"name": "mouth_1", "type": "opening", "face": "wall",
             "centre_m": (0.0, 0.0, 0.0), "diameter_m": 0.0118}]
    topo, patch = render_port_patch_dicts(rows, "wall")
    assert "patch wall;" in topo
    assert "name mouth_1;" in patch


def test_the_cut_dict_refuses_a_face_that_is_neither_a_block_face_nor_this_runs_shell():
    """The validation still validates. On a run whose shell is `wall`, a row left saying
    `wall_outer` is exactly the stale-name bug above, and it must stop the run rather than cut
    nothing."""
    rows = [{"name": "mouth_1", "type": "opening", "face": OUTER_PATCH,
             "centre_m": (0.0, 0.0, 0.0), "diameter_m": 0.0118}]
    with pytest.raises(ConjugateError):
        render_port_patch_dicts(rows, "wall")


def test_a_run_that_named_nothing_still_cuts_out_of_the_engines_own_shell():
    """The control: no run-specific name, and everything behaves as it did before."""
    rows = [{"name": "mouth_1", "type": "opening", "face": OUTER_PATCH,
             "centre_m": (0.0, 0.0, 0.0), "diameter_m": 0.0118}]
    topo, _ = render_port_patch_dicts(rows)
    assert f"patch {OUTER_PATCH};" in topo
    assert is_domain_face(OUTER_PATCH) and is_domain_face("xmin")
    assert is_domain_face("wall", "wall") and not is_domain_face("wall")


# ---- the gate ---------------------------------------------------------------------------------

class _Ctx:
    def __init__(self, workspace, manifest, intake_patches=()):
        self.workspace = str(workspace)
        self._m = manifest
        self.intake_patches = list(intake_patches)

    def manifest_or_load(self):
        return self._m


def _man(patch_types, planned=()):
    return {"schema_version": "2.1", "patch_types": dict(patch_types),
            "quality": {"port_patches_planned": list(planned)}}


def _contract_gate(ws, patch_types, planned=(), declared=DECLARED_WALL):
    gate = next(g for g in G.CONJUGATE_GATES if g.key == "patch_contract")
    return gate.check(_Ctx(ws, _man(patch_types, planned), declared))


def test_the_gate_passes_the_mesh_job_2b72862e_would_now_deliver(tmp_path):
    """The whole point. Identical to the refused manifest except the outer patch wears the name
    the user approved - and the delivered OpenFOAM type was ALREADY `wall`, so name and type
    both agree and the contract is met exactly, with no tolerance anywhere."""
    ok, said = _contract_gate(tmp_path,
                              {"wall": "wall", "mouth_2": "patch", "mouth_3": "patch"},
                              planned=["mouth_1", "mouth_2", "mouth_3"])
    assert ok, said


def test_the_gate_still_refuses_the_mesh_job_2b72862e_actually_delivered(tmp_path):
    """The refusal was RIGHT, and must stay right: a mesh with no boundary called `wall` does not
    give the user what they approved, whatever else is in it."""
    ok, said = _contract_gate(tmp_path,
                              {"wall_outer": "wall", "mouth_2": "patch", "mouth_3": "patch"},
                              planned=["mouth_1", "mouth_2", "mouth_3"])
    assert not ok and "wall" in said, said


def test_a_patch_the_engine_never_planned_is_still_an_extra(tmp_path):
    """The exemption is keyed on the engine's OWN record of what it cut. A patch nobody asked for
    and no plan accounts for is still refused."""
    ok, said = _contract_gate(tmp_path, {"wall": "wall", "mystery": "patch"}, planned=[])
    assert not ok and "mystery" in said, said


def test_an_opening_shaped_name_the_plan_did_not_cut_is_still_an_extra(tmp_path):
    """Keyed on the plan, never on the NAME. `mouth_4` looks exactly like the engine's own work
    and is not in the plan, so it is an extra."""
    ok, said = _contract_gate(tmp_path, {"wall": "wall", "mouth_4": "patch"},
                              planned=["mouth_1", "mouth_2"])
    assert not ok and "mouth_4" in said, said


def test_a_declared_name_is_never_exempted_even_when_the_plan_also_cut_it(tmp_path):
    """THE SAFETY PROPERTY, and the one an over-broad filter would destroy. If a user declared a
    name the plan happens to carry too, the exemption must not hide it: a mesh that LOST it is
    still a mesh missing a boundary the user approved."""
    declared = [{"name": "wall", "type": "wall"}, {"name": "mouth_2", "type": "inlet"}]
    ok, said = _contract_gate(tmp_path, {"wall": "wall"},
                              planned=["mouth_1", "mouth_2"], declared=declared)
    assert not ok and "mouth_2" in said, said


def test_the_exemption_cannot_pass_a_mesh_with_no_patches_at_all(tmp_path):
    """A delivered-side filter that removed everything must refuse, not pass vacuously."""
    ok, said = _contract_gate(tmp_path, {"mouth_2": "patch"}, planned=["mouth_2"])
    assert not ok, said


def test_a_declared_type_that_disagrees_with_the_delivered_one_is_still_refused(tmp_path):
    """Name equality is not enough: the gate compares types too, and must keep doing so."""
    ok, said = _contract_gate(tmp_path, {"wall": "patch"}, planned=[])
    assert not ok, said
