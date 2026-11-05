# Responsibility: Verify the multi-region snappy case maps solids to safe region names and renders per-region dicts.
from __future__ import annotations

from pathlib import Path

from meshpipeline.engines import registry as ec  # noqa: E402
from meshpipeline.engines.snappy_multiregion import authoring, multiregion_runner  # noqa: E402

_REGIONS = [
    {"name": "coolant", "type": "fluid", "solids": [0]},
    {"name": "block", "type": "solid", "solids": [1]},
    {"name": "chip", "type": "solid", "solids": [2, 3]},
]


# spec / registry

def test_snappy_multiregion_is_an_implemented_engine():
    assert "snappy_multiregion" in ec.engine_names()
    sp = ec.get_spec("snappy_multiregion")
    assert sp.implemented
    assert sp.capabilities == (("solid-assembly", "multiregion-volume"),) or \
        [(c.input_kind, c.output_kind) for c in sp.capabilities] == [("solid-assembly", "multiregion-volume")]
    # the descriptor is CAPABILITY-first, not domain-tied: it leads with the physical
    # capability (multi-region body-fitted meshing) and treats domains as EXAMPLE purposes,
    # never as the engine's identity - the same rule gmsh/snappy follow.
    desc = sp.descriptor.lower()
    assert "multi-region" in desc and "body-fitted" in desc
    example_domains = sum(d in desc for d in
                          ("conjugate heat transfer", "multi-material", "fluid-structure"))
    assert example_domains >= 2, "domains must appear as several examples, not one identity"
    assert not desc.startswith("thermal") and "cht meshing" not in desc


def test_the_domain_lives_in_the_purpose_not_the_engine():
    from meshpipeline.engines.purposes import PURPOSES
    assert "conjugate_heat_transfer" in PURPOSES          # the domain is a purpose
    assert "cht" not in ec.get_spec("snappy_multiregion").name  # not in the engine name


def test_configure_mesh_palette_keys_are_taught_in_the_prompt():
    sp = ec.get_spec("snappy_multiregion")
    props = sp.authoring_tool["function"]["parameters"]["properties"]
    prompt = sp.system_prompt
    missing = [k for k in props if k not in prompt]
    assert not missing, f"palette keys not taught: {missing}"


# region map

def test_region_map_normalises_and_dedups_solids():
    rmap = multiregion_runner.region_map(_REGIONS)
    assert list(rmap) == ["coolant", "block", "chip"]
    assert multiregion_runner.fluid_regions(rmap) == ["coolant"]
    assert multiregion_runner.solid_regions(rmap) == ["block", "chip"]
    assert rmap["chip"]["solids"] == [2, 3]


def test_region_map_makes_names_openfoam_safe():
    rmap = multiregion_runner.region_map([{"name": "hot side!", "type": "solid", "solids": [0]}])
    assert list(rmap) == ["hot_side"]


# pure renderers

def test_region_properties_partitions_fluid_and_solid():
    txt = multiregion_runner.render_region_properties(_REGIONS)
    assert "fluid       (coolant)" in txt
    assert "block chip" in txt and "solid       (block chip)" in txt
    assert 'object regionProperties;' in txt


def test_refinement_surfaces_tag_a_cellzone_per_region_and_refine_solids():
    rmap = multiregion_runner.region_map(_REGIONS)
    block = multiregion_runner.render_refinement_surfaces(rmap, (2, 2), interface_refinement=1)
    # every region tags its own cellZone with cells-inside assignment
    for name in ("coolant", "block", "chip"):
        assert f"cellZone {name};" in block
    assert block.count("cellZoneInside inside;") == 3
    # solids get +interface_refinement levels; the fluid does not
    assert "level (2 3);" in block   # block/chip (solid) -> 2+1
    assert "level (2 2);" in block   # coolant (fluid)


def test_snappy_multiregion_dict_has_multiregion_snap_and_layers_on_interfaces():
    rmap = multiregion_runner.region_map(_REGIONS)
    d = multiregion_runner.render_snappy_multiregion_dict(
        rmap, (0, 0, 0), (1, 1, 1), surface_level=(2, 2), interface_refinement=1,
        n_layers=3, first_layer_rel=0.35, quality="balanced", max_cells=4_000_000)
    assert "multiRegionFeatureSnap true;" in d
    assert "locationInMesh (0.5 0.5 0.5);" in d
    # prism layers sit on the fluid side of each fluid-solid interface
    assert '"coolant_to_block" { nSurfaceLayers 3; }' in d
    assert '"coolant_to_chip" { nSurfaceLayers 3; }' in d
    assert "maxGlobalCells  4000000;" in d


def test_strict_quality_tightens_skew_and_nonortho():
    rmap = multiregion_runner.region_map(_REGIONS)
    d = multiregion_runner.render_snappy_multiregion_dict(
        rmap, (0, 0, 0), (1, 1, 1), surface_level=(2, 2), interface_refinement=0,
        n_layers=0, first_layer_rel=0.35, quality="strict", max_cells=1_000_000)
    assert "maxInternalSkewness 3.5;" in d and "maxNonOrtho 65;" in d
    assert "addLayers       false;" in d   # n_layers 0


def test_block_mesh_cell_count_tracks_base_cell():
    txt = multiregion_runner.render_block_mesh((0, 0, 0), (1, 1, 1), base_cell=0.1)
    # a 1 m box padded 15% each side ~1.3 m / 0.1 ~ 13 cells
    assert "hex (0 1 2 3 4 5 6 7) (13 13 13)" in txt


# interface conformality parse

def _write_boundary(p: Path, patches: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"{n} {{ type wall; nFaces {nf}; startFace 0; }}" for n, nf in patches.items())
    p.write_text(f"{len(patches)}\n(\n{body}\n)\n")


def test_check_interfaces_passes_on_matching_face_counts(tmp_path):
    rmap = multiregion_runner.region_map([
        {"name": "coolant", "type": "fluid", "solids": [0]},
        {"name": "block", "type": "solid", "solids": [1]},
    ])
    _write_boundary(tmp_path / "constant" / "coolant" / "polyMesh" / "boundary",
                    {"inlet": 20, "coolant_to_block": 128})
    _write_boundary(tmp_path / "constant" / "block" / "polyMesh" / "boundary",
                    {"block_to_coolant": 128, "external": 64})
    res = multiregion_runner.check_interfaces(tmp_path, rmap)
    assert res["interface_ok"] is True
    assert res["interface_mismatch"] == []


def test_check_interfaces_flags_mismatched_face_counts(tmp_path):
    rmap = multiregion_runner.region_map([
        {"name": "coolant", "type": "fluid", "solids": [0]},
        {"name": "block", "type": "solid", "solids": [1]},
    ])
    _write_boundary(tmp_path / "constant" / "coolant" / "polyMesh" / "boundary",
                    {"coolant_to_block": 128})
    _write_boundary(tmp_path / "constant" / "block" / "polyMesh" / "boundary",
                    {"block_to_coolant": 100})   # mismatch
    res = multiregion_runner.check_interfaces(tmp_path, rmap)
    assert res["interface_ok"] is False
    assert "coolant_to_block" in res["interface_mismatch"]


# authoring validator

def test_authoring_accepts_a_valid_cht_strategy():
    assert authoring.validate({"regions": _REGIONS, "surface_level": [2, 3],
                               "interface_refinement": 1, "n_layers": 3}) == []


def test_authoring_requires_at_least_one_fluid_and_one_solid():
    d = authoring.validate({"regions": [{"name": "a", "type": "fluid", "solids": [0]}]})
    assert any("at least one 'solid'" in x.message for x in d)
    d2 = authoring.validate({"regions": [{"name": "a", "type": "solid", "solids": [0]}]})
    assert any("at least one 'fluid'" in x.message for x in d2)


def test_authoring_rejects_a_solid_in_two_regions():
    d = authoring.validate({"regions": [
        {"name": "f", "type": "fluid", "solids": [0]},
        {"name": "s", "type": "solid", "solids": [0]},   # 0 already owned
    ]})
    assert any("already assigned" in x.message for x in d)


def test_authoring_rejects_missing_regions_and_single_region_knobs():
    assert any(x.path == "regions" for x in authoring.validate({"surface_level": [2, 2]}))
    d = authoring.validate({"regions": _REGIONS, "domain_margin": {"up": 5}})
    assert any("single-region snappy knob" in x.message for x in d)
