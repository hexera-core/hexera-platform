# Responsibility: Verify an ECXML thermal model (JEDEC JEP181A, schema Rev 2.0) is read safely and becomes named solids plus the air, at its true size, with its physics kept beside it.
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from tests.unit.cad.ecxml_models import Ecxml, ducted_board, set_top_box, tiny_board

from meshpipeline.cad.ingest import canonicalise, check_upload, sniff_format
from meshpipeline.cad.ingest import ecxml as ecxml_mod
from meshpipeline.cad.ingest.ecxml import EcxmlError, read_ecxml
from meshpipeline.contracts.geometry_units import LengthUnit
from meshpipeline.contracts.intake_formats import GeometryKind, format_for_suffix

HEAD = b'<?xml version="1.0" encoding="UTF-8"?>\n'


def _doc(geometry: str = "", *, root: str = "neutralXML", head: bytes = HEAD,
         extra: str = "") -> bytes:
    return head + (f"<{root}><name>m</name><producer>FloTHERM</producer>{extra}"
                   f"<geometry>{geometry}</geometry></{root}>").encode()


def _block(name="B", lo=(0, 0, 0), size=(0.01, 0.01, 0.01), material="M", power=0.0,
           active=True) -> str:
    xyz = lambda tag, v: f"<{tag}>" + "".join(f"<{a}>{x}</{a}>" for a, x in zip("xyz", v)) \
        + f"</{tag}>"  # noqa: E731
    return (f"<solid3dBlock><name>{name}</name><active>{str(active).lower()}</active>"
            f"{xyz('location', lo)}{xyz('size', size)}<material>{material}</material>"
            f"<powerDissipation>{power}</powerDissipation></solid3dBlock>")


def _write(tmp_path: Path, data: bytes, name: str = "model.ecxml") -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


# ------------------------------------------------------------------------------ the format ---
def test_ecxml_is_declared_as_cad_that_states_its_unit():
    fmt = format_for_suffix(".ecxml")
    assert fmt is not None and fmt.key == "ecxml" and fmt.kind is GeometryKind.cad
    assert fmt.declares_units and not fmt.canonical
    # Icepak names its export <project>_ec.xml: the .xml name is read by content, like any other
    assert format_for_suffix(".xml") is fmt


def test_the_sample_models_hold_every_kind_of_object_the_schema_defines():
    kinds = set()
    for make in (set_top_box, ducted_board, tiny_board):
        kinds |= {o.kind for o in read_ecxml(make().xml()).objects}
    # all 16 geometry elements of ECXML Rev 2.0 except the external MCAD reference, tested apart
    assert kinds == set(ecxml_mod.OBJECT_FIELDS) - {"externalMcadFile"}


def test_the_sample_models_validate_against_the_official_schema(tmp_path):
    """The JEDEC schema (ECXML_Rev2.0.xsd, from JEP181A) is JEDEC's copyright and is not in this
    repository; point ECXML_XSD at a licensed copy to run this check."""
    import os

    xsd = os.environ.get("ECXML_XSD")
    if not xsd or not Path(xsd).is_file():
        pytest.skip("ECXML_XSD does not name the official ECXML Rev 2.0 schema")
    etree = pytest.importorskip("lxml.etree")
    schema = etree.XMLSchema(etree.parse(xsd))
    doc = Ecxml("all", producer="Icepak").domain((0, 0, 0), (0.1, 0.1, 0.1))
    doc.mcad("Bracket", (0, 0, 0), "bracket.step", "M", 1.0)
    for data in (set_top_box().xml(), ducted_board().xml(), tiny_board().xml(), doc.xml()):
        assert schema.validate(etree.fromstring(data)), schema.error_log


# ------------------------------------------------------------------------------ reading ------
def test_location_is_the_minimum_corner_and_every_value_stays_si():
    model = read_ecxml(tiny_board().xml())
    chip = next(o for o in model.objects if o.name == "Chip")
    assert chip.lo == pytest.approx((0.02, 0.015, 0.0066))
    assert chip.hi == pytest.approx((0.03, 0.025, 0.0086))
    assert chip.power == 2.0 and chip.material == "Alu"
    assert model.domain is not None and model.domain.size == pytest.approx((0.05, 0.04, 0.02))
    assert model.domain.ambient == {"temperature_K": 298.15, "radiant_temperature_K": 298.15,
                                    "pressure_Pa": 0.0}


def test_an_assembly_only_groups_its_objects_keep_global_coordinates():
    doc = Ecxml("nest")
    with doc.assembly("Outer"):
        with doc.assembly("Inner"):
            doc.block("Deep", (0.1, 0.2, 0.3), (0.01, 0.01, 0.01), "M")
    deep = next(o for o in read_ecxml(doc.xml()).objects if o.name == "Deep")
    assert deep.path == ("Outer", "Inner") and deep.lo == pytest.approx((0.1, 0.2, 0.3))


def test_every_conductivity_kind_is_kept():
    mats = read_ecxml(set_top_box().xml()).materials
    assert mats["Aluminium 6063"].conductivity == {"kind": "isotropic", "W_mK": 201.0}
    assert mats["FR4 board"].conductivity["W_mK"] == [17.0, 17.0, 0.4]
    assert mats["Steel sheet"].conductivity["kind"] == "linear_in_temperature"
    assert mats["Silicon"].conductivity["points_K_W_mK"] == [[250.0, 190.0], [300.0, 150.0],
                                                             [400.0, 100.0]]
    assert mats["Mold compound"].density == 1900 and mats["Mold compound"].emissivity == 0.9


def test_fans_keep_their_flow_definition():
    objs = {o.name: o for o in read_ecxml(set_top_box().xml()).objects}
    fan = objs["Exhaust fan"]
    assert fan.props["flow"]["kind"] == "fan_curve" and fan.props["hub_diameter_m"] == 0.01
    assert fan.props["flow"]["points_m3_s_Pa"][0] == [0.0, 45.0]
    objs = {o.name: o for o in read_ecxml(ducted_board().xml()).objects}
    assert objs["Inlet fan"].props["flow"] == {"kind": "fixed_flow_rate", "m3_s": 0.004}


def test_an_inactive_object_and_everything_in_an_inactive_assembly_are_left_out():
    doc = Ecxml("inactive")
    doc.block("Off", (0, 0, 0), (0.01, 0.01, 0.01), "M", active=False)
    with doc.assembly("Parked", active=False):
        doc.block("Inside", (0, 0, 0), (0.01, 0.01, 0.01), "M")
    doc.block("On", (0, 0, 0), (0.01, 0.01, 0.01), "M")
    model = read_ecxml(doc.xml())
    assert [o.name for o in model.active_objects] == ["On"]


def test_tags_are_read_without_regard_to_capitals_or_namespace_prefixes():
    data = _doc(_block().replace("solid3dBlock", "Solid3DBlock"))
    data = data.replace(b"<neutralXML>", b'<e:neutralXML xmlns:e="urn:x">').replace(
        b"</neutralXML>", b"</e:neutralXML>")
    model = read_ecxml(data)
    assert [o.kind for o in model.objects] == ["solid3dBlock"]


def test_vendor_extensions_are_counted_and_never_read():
    data = _doc(_block().replace("</solid3dBlock>", "<vendorThing>9</vendorThing></solid3dBlock>")
                + "<unknownObject><name>x</name></unknownObject>")
    model = read_ecxml(data)
    assert model.ignored_elements == {"solid3dBlock/vendorThing": 1, "geometry/unknownObject": 1}


@pytest.mark.parametrize("producer", ["Flotherm", "6SigmaET", "Celsius EC"])
def test_a_producer_the_schema_does_not_list_is_read_the_same_way(producer):
    model = read_ecxml(_doc(_block()).replace(b"FloTHERM", producer.encode()))
    assert model.producer == producer and any("producer" in n for n in model.notes)


@pytest.mark.parametrize("change, said", [
    (("<powerDissipation>0.0</powerDissipation>", ""), "no <powerDissipation>"),
    (("<x>0.01</x>", "<x>abc</x>"), "not a number"),
    (("<x>0.01</x>", "<x>NaN</x>"), "finite"),
    (("<x>0.01</x>", "<x>INF</x>"), "finite"),
    (("<x>0.01</x>", "<x>-0.01</x>"), "greater than 0"),
    (("<active>true</active>", "<active>maybe</active>"), "true, false"),
])
def test_a_value_the_schema_rules_out_is_refused_saying_where(change, said):
    data = _doc(_block(name="U7")).replace(change[0].encode(), change[1].encode(), 1)
    with pytest.raises(EcxmlError) as err:
        read_ecxml(data)
    assert said in str(err.value) and "U7" in str(err.value)


def test_a_ratio_above_one_and_an_unknown_plane_are_refused():
    doc = Ecxml("r")
    doc.grille("G", (0, 0, 0), (0.01, 0.01, 0.001), "+xy", 1.0, 0.5)
    with pytest.raises(EcxmlError, match="at most 1"):
        read_ecxml(doc.xml().replace(b"<freeAreaRatio>0.50000000", b"<freeAreaRatio>1.5"))
    with pytest.raises(EcxmlError, match="one of"):
        read_ecxml(doc.xml().replace(b"<plane>+xy", b"<plane>+zx"))


def test_a_file_that_is_not_ecxml_says_so():
    with pytest.raises(EcxmlError, match="not an ECXML"):
        read_ecxml(_doc(_block(), root="model"))
    with pytest.raises(EcxmlError, match="not well-formed"):
        read_ecxml(b"<neutralXML><name>x</neutralXML>")


def test_utf16_ecxml_is_recognised_and_read(tmp_path):
    text = tiny_board().xml().decode().replace('encoding="UTF-8"', 'encoding="UTF-16"')
    src = _write(tmp_path, text.encode("utf-16"))
    assert sniff_format(src) == "ecxml"
    assert {o.name for o in read_ecxml(src).objects} == {"Board", "Chip"}


# ------------------------------------------------------------------------------ hostile ------
BILLION_LAUGHS = (b'<?xml version="1.0"?><!DOCTYPE neutralXML [<!ENTITY a "aaaaaaaaaa">'
                  b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;"><!ENTITY c "&b;&b;&b;&b;&b;&b;">]>'
                  b"<neutralXML><name>&c;</name></neutralXML>")


def test_a_dtd_with_expanding_entities_is_refused_before_anything_expands():
    with pytest.raises(EcxmlError, match="DTD"):
        read_ecxml(BILLION_LAUGHS)


def test_an_external_entity_is_never_fetched(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET")
    data = (b'<?xml version="1.0"?><!DOCTYPE neutralXML [<!ENTITY x SYSTEM "'
            + secret.as_uri().encode() + b'">]><neutralXML><name>&x;</name></neutralXML>')
    with pytest.raises(EcxmlError) as err:
        read_ecxml(data)
    assert "TOPSECRET" not in str(err.value)
    # without a DTD an entity reference is simply undefined: still refused, still not read
    with pytest.raises(EcxmlError, match="well-formed"):
        read_ecxml(b"<neutralXML><name>&x;</name></neutralXML>")


def test_a_parameter_entity_is_refused():
    data = (b'<?xml version="1.0"?><!DOCTYPE neutralXML [<!ENTITY % p SYSTEM "http://x.invalid/">'
            b"%p;]><neutralXML/>")
    with pytest.raises(EcxmlError):
        read_ecxml(data)


def test_nesting_past_the_depth_bound_is_refused():
    deep = "<assembly><name>a</name><active>true</active><geometry>" * 120 \
        + "</geometry></assembly>" * 120
    with pytest.raises(EcxmlError, match="deeper"):
        read_ecxml(_doc(deep))


def test_the_size_count_and_text_bounds_hold(tmp_path, monkeypatch):
    data = _doc(_block() * 20)
    monkeypatch.setattr(ecxml_mod, "MAX_ECXML_BYTES", 1000)
    with pytest.raises(EcxmlError, match="larger than"):
        read_ecxml(_write(tmp_path, data))
    monkeypatch.setattr(ecxml_mod, "MAX_ECXML_BYTES", 10 ** 9)
    monkeypatch.setattr(ecxml_mod, "MAX_ELEMENTS", 50)
    with pytest.raises(EcxmlError, match="XML elements"):
        read_ecxml(data)
    monkeypatch.setattr(ecxml_mod, "MAX_ELEMENTS", 10 ** 7)
    monkeypatch.setattr(ecxml_mod, "MAX_TEXT_CHARS", 100)
    with pytest.raises(EcxmlError, match="characters"):
        read_ecxml(_doc(_block(name="x" * 500)))


# ------------------------------------------------------------------------------ sniff, upload -
def test_ecxml_is_known_by_its_root_whatever_its_name(tmp_path):
    data = set_top_box().xml()
    for name in ("box.ecxml", "box_ec.xml", "named_wrong.stl"):
        assert sniff_format(_write(tmp_path, data, name)) == "ecxml"
    commented = data.replace(b"<neutralXML>", b"<!-- a > b --><neutralXML>")
    assert sniff_format(_write(tmp_path, commented, "c.ecxml")) == "ecxml"
    assert sniff_format(_write(tmp_path, b'<?xml version="1.0"?><config/>', "o.xml")) is None


def test_upload_accepts_ecxml_and_refuses_what_is_not(tmp_path):
    assert check_upload(_write(tmp_path, set_top_box().xml()), ".ecxml").ok
    assert check_upload(_write(tmp_path, set_top_box().xml(), "m_ec.xml"), ".xml").ok
    other = check_upload(_write(tmp_path, b'<?xml version="1.0"?><config/>', "c.xml"), ".xml")
    assert not other.ok and "not a JEDEC JEP181 ECXML thermal model file" in other.refusal
    hostile = check_upload(_write(tmp_path, BILLION_LAUGHS, "h.ecxml"), ".ecxml")
    assert not hostile.ok and "DTD" in hostile.refusal
    broken = check_upload(_write(tmp_path, _doc(_block()).replace(b"<x>0.01</x>", b"<x>?</x>", 1),
                                 "b.ecxml"), ".ecxml")
    assert not broken.ok and "not a number" in broken.refusal


# ------------------------------------------------------------------------------ the solids ---
@pytest.fixture(scope="module")
def stb(tmp_path_factory):
    out = tmp_path_factory.mktemp("stb")
    src = out / "source.ecxml"
    src.write_bytes(set_top_box().xml())
    canonical = canonicalise(src, out, stem="source")
    side = json.loads(Path(str(canonical.path) + ".ecxml.json").read_text())
    return {"canonical": canonical, "side": side, "dir": out}


def _regions(side) -> dict:
    return {r["name"]: r for r in side["regions"]}


def _box_volume(side) -> float:
    b = side["domain"]["box_m"]
    return math.prod(b["max"][i] - b["min"][i] for i in range(3))


def test_a_vented_box_becomes_named_solids_and_one_air_region(stb):
    canonical, side = stb["canonical"], stb["side"]
    assert canonical.kind is GeometryKind.cad and canonical.path.suffix == ".step"
    assert canonical.source_format == "ecxml" and canonical.converted
    regions = _regions(side)
    assert list(canonical.regions) == [r["name"] for r in side["regions"]]
    assert set(regions) == {"air", "Case", "PCB", "SoC", "SoC_heat_sink", "DDR_1", "DDR_2",
                            "PMIC", "Bulk_cap"}
    assert [r["name"] for r in side["regions"] if r["type"] == "fluid"] == ["air"]
    # the regions tile the domain exactly: no overlap, no gap
    assert sum(r["volume_m3"] for r in side["regions"]) == pytest.approx(_box_volume(side),
                                                                         rel=1e-9)
    assert regions["PCB"]["volume_m3"] == pytest.approx(0.09 * 0.07 * 0.0016, rel=1e-9)
    assert regions["SoC"]["power_W"] == 6.0 and regions["SoC"]["material"] is None
    assert regions["DDR_1"]["material"] == "Mold compound"


def test_the_vents_open_the_enclosure_walls_they_sit_in(stb):
    case = _regions(stb["side"])["Case"]
    shell = 0.12 * 0.09 * 0.035 - (0.12 - 0.005) * (0.09 - 0.005) * (0.035 - 0.005)
    grille_hole = 0.0025 * 0.06 * 0.012
    fan_hole = math.pi * 0.0125 ** 2 * 0.0025
    assert case["volume_m3"] == pytest.approx(shell - grille_hole - fan_hole, rel=1e-6)
    assert any("opened where" in n for n in case["notes"])


def test_the_heat_sink_is_one_solid_of_its_base_and_fins(stb):
    hs = _regions(stb["side"])["SoC_heat_sink"]
    assert hs["kind"] == "heatsink" and len(hs["objects"]) == 7
    assert hs["volume_m3"] == pytest.approx(0.02 * 0.02 * 0.002 + 6 * 0.0015 * 0.02 * 0.012,
                                            rel=1e-6)


def test_the_cylinder_fills_its_bounding_box(stb):
    cap = _regions(stb["side"])["Bulk_cap"]
    assert cap["volume_m3"] == pytest.approx(math.pi * 0.004 ** 2 * 0.011, rel=1e-6)


def test_the_physics_is_kept_beside_the_geometry(stb):
    side = stb["side"]
    assert side["spec"].startswith("JEDEC JEP181A") and side["units"]["length"] == "m"
    assert side["domain"]["ambient"]["temperature_K"] == 308.15
    assert side["total_power_W"] == pytest.approx(6.0 + 0.6 + 0.6 + 0.9 + 0.1 + 0.5)
    assert [f["name"] for f in side["fans"]] == ["Exhaust fan"]
    assert [g["name"] for g in side["grilles"]] == ["Front vent"]
    assert side["compact_models"][0]["case_face"] == "+z"
    assert side["compact_models"][0]["board_face"] == "-z"
    assert [s["name"] for s in side["volume_heat_sources"]] == ["SoC die heat"]
    assert {m["name"] for m in side["monitor_points"]} == {"Tj SoC", "Air exhaust"}
    assert set(side["materials"]) == {"Aluminium 6063", "FR4 board", "Mold compound",
                                      "Steel sheet", "Silicon"}
    facts = stb["canonical"].facts()["thermal_model"]
    assert facts["regions"][0] == {"name": "air", "type": "fluid", "kind": "air"}
    assert facts["counts"]["fans"] == 1


def test_the_step_names_every_region_in_millimetres(stb):
    from meshpipeline.cad.ingest.cad import read_step, shape_stats
    from meshpipeline.cad.regions import regions_of
    from meshpipeline.cad.unit_evidence import parser_applied_unit, read_declared_unit

    path = stb["canonical"].path
    names = regions_of(path)
    assert names.names == stb["canonical"].regions and names.source == "roots"
    st = shape_stats(read_step(path))
    assert st["solids"] == len(names.names)
    assert st["bounds_min"] == pytest.approx([-30.0, -30.0, -20.0], abs=1e-6)
    assert st["bounds_max"] == pytest.approx([150.0, 120.0, 60.0], abs=1e-6)
    # the file's metres, built in millimetres (OpenCASCADE's fixed tolerances are then 0.1 nm)
    assert parser_applied_unit(path) is LengthUnit.millimetre
    evidence = read_declared_unit(path)
    assert evidence.resolved and evidence.unit is LengthUnit.millimetre


def test_an_ecxml_meshes_at_its_true_size(stb, tmp_path):
    from tests._geometry_support import interpretation_ref, source_ref

    from meshpipeline.application.geometry_materializer import canonical_path
    from meshpipeline.cad.staging import _consumed_state
    from meshpipeline.cad.unit_evidence import read_declared_unit
    from meshpipeline.contracts.geometry_source import MaterializedGeometry

    src = tmp_path / "source.ecxml"
    src.write_bytes(tiny_board().xml())
    declared = read_declared_unit(src)          # what the upload records
    assert declared.resolved and declared.unit is LengthUnit.millimetre
    ref = source_ref(local_file=src, filename="board.ecxml")
    geom = MaterializedGeometry(
        ref=ref, interpretation=interpretation_ref(geometry_source_id=ref.source_id,
                                                   unit=declared.unit),
        local_path=canonical_path(src))
    assert geom.local_path == tmp_path / "source.step"
    assert _consumed_state(geom, geom.local_path.suffix).to_metres == pytest.approx(1e-3)


def test_the_multiregion_report_names_each_solid(stb, tmp_path):
    from meshpipeline.contracts.coordinate_state import from_occ_transfer
    from meshpipeline.contracts.geometry_units import (
        GeometryInterpretation,
        ResolutionBasis,
    )
    from meshpipeline.engines.snappy_multiregion.multiregion_runner import read_assembly_solids

    interp = GeometryInterpretation(
        interpretation_id="i", owner_id="o", geometry_source_id="s", unit=LengthUnit.millimetre,
        scale_to_metres=1e-3, basis=ResolutionBasis.file_declared, evidence="ecxml")
    solids = read_assembly_solids(stb["canonical"].path, tmp_path / "asm",
                                  prepared=from_occ_transfer(interp, LengthUnit.millimetre))
    by_name = {s.get("name"): s for s in solids}
    assert set(by_name) == set(stb["canonical"].regions)
    pcb = _regions(stb["side"])["PCB"]
    assert by_name["PCB"]["centroid"] == pytest.approx(pcb["centroid_m"], abs=1e-6)


# ------------------------------------------------------------------------------ the rules ----
def _build(doc: Ecxml):
    from meshpipeline.cad.ingest.ecxml_build import build

    return build(read_ecxml(doc.xml()))


def _vol(result, name) -> float:
    return next(r["volume_m3"] for r in result.sidecar["regions"] if r["name"] == name)


def test_where_solids_overlap_the_later_one_wins():
    doc = Ecxml("overlap")
    doc.domain((0, 0, 0), (0.1, 0.1, 0.1))
    doc.block("First", (0.0, 0.0, 0.0), (0.04, 0.04, 0.04), "M")
    doc.block("Second", (0.02, 0.0, 0.0), (0.04, 0.04, 0.04), "M")
    r = _build(doc)
    assert _vol(r, "Second") == pytest.approx(0.04 ** 3, rel=1e-9)
    assert _vol(r, "First") == pytest.approx(0.02 * 0.04 * 0.04, rel=1e-9)


def test_for_icepak_a_solid_inside_another_wins_whatever_the_order():
    doc = Ecxml("embedded", producer="Icepak")
    doc.domain((0, 0, 0), (0.1, 0.1, 0.1))
    doc.block("Inner", (0.02, 0.02, 0.02), (0.01, 0.01, 0.01), "M")
    doc.block("Housing", (0.0, 0.0, 0.0), (0.05, 0.05, 0.05), "M")
    r = _build(doc)
    assert _vol(r, "Inner") == pytest.approx(1e-6, rel=1e-9)
    assert _vol(r, "Housing") == pytest.approx(0.05 ** 3 - 1e-6, rel=1e-9)
    # the same file from Flotherm: the later Housing overwrites Inner entirely
    flo = _build(Ecxml("embedded").domain((0, 0, 0), (0.1, 0.1, 0.1))
                 .block("Inner", (0.02, 0.02, 0.02), (0.01, 0.01, 0.01), "M")
                 .block("Housing", (0.0, 0.0, 0.0), (0.05, 0.05, 0.05), "M"))
    assert "Inner" not in flo.region_names
    assert any(n.startswith("Overlap: Housing overwrite(s) all of Inner") for n in flo.notes)


def test_a_sealed_enclosure_keeps_its_own_air():
    doc = Ecxml("sealed")
    doc.domain((-0.01, -0.01, -0.01), (0.07, 0.07, 0.07))
    doc.enclosure("Box", (0, 0, 0), (0.05, 0.05, 0.05), "M", 0.002)
    r = _build(doc)
    fluids = [x for x in r.sidecar["regions"] if x["type"] == "fluid"]
    assert [f["name"] for f in fluids] == ["air", "air_2"]
    assert fluids[1]["volume_m3"] == pytest.approx(0.046 ** 3, rel=1e-9)


def test_2d_objects_on_the_domain_faces_are_patches_pointing_the_way_they_say():
    side = _build(ducted_board()).sidecar
    patches = {p["name"]: p for p in side["patches"]}
    assert patches["Inlet fan"]["suggested_type"] == "inlet"           # -xz on the +y face
    assert patches["Inlet fan"]["domain_face"] == "+y"
    assert patches["Outlet"]["role"] == "vent" and patches["Outlet"]["domain_face"] == "-y"
    assert {patches[n]["role"] for n in ("Low X", "High X", "Low Z", "High Z")} == {"wall"}
    # every side is wholly covered by an object, so no bare domain side is left
    assert not [p for p in side["patches"] if p["role"] == "domain_boundary"]
    doc = Ecxml("out").domain((0, 0, 0), (0.1, 0.1, 0.1))
    doc.fan2d("Puller", (0.1, 0.02, 0.02), (0.0, 0.03, 0.03), "+yz", 0.001)
    doc.block("B", (0.01, 0.01, 0.01), (0.02, 0.02, 0.02), "M")
    pulled = {p["name"]: p for p in _build(doc).sidecar["patches"]}
    assert pulled["Puller"]["suggested_type"] == "outlet"
    assert pulled["domain_xmax"]["partly_covered_by"] == ["Puller"]


def test_a_plate_is_a_thin_solid_only_with_a_thickness_a_plate_can_have():
    doc = Ecxml("plates").domain((0, 0, 0), (0.1, 0.1, 0.1))
    doc.plate("Shield", (0.01, 0.05, 0.01), (0.05, 0.001, 0.03), "+xz", "M")
    doc.plate("Sheet", (0.01, 0.07, 0.01), (0.05, 0.0, 0.03), "+xz", "M")
    doc.plate("Odd", (0.01, 0.02, 0.01), (0.05, 3333.0, 0.03), "+xz", "M")
    r = _build(doc)
    assert _vol(r, "Shield") == pytest.approx(0.05 * 0.001 * 0.03, rel=1e-9)
    assert {b["name"] for b in r.sidecar["baffles"]} == {"Sheet", "Odd"}
    # the normal size of a 2D object never stretches the domain
    no_domain = Ecxml("nd").block("B", (0, 0, 0), (0.02, 0.02, 0.02), "M")
    no_domain.source2d("S", (0.0, 0.0, 0.02), (0.02, 0.02, 3333.0), "+xy", 1.0)
    side = _build(no_domain).sidecar
    assert side["domain"]["box_m"]["max"] == pytest.approx([0.02, 0.02, 0.02])
    assert "bounding box" in side["domain"]["source"]


def test_an_elliptical_cylinder_and_parts_outside_the_domain():
    doc = Ecxml("cyl").domain((0, 0, 0), (0.1, 0.1, 0.1))
    doc.cylinder("Ell", (0.01, 0.01, 0.01), (0.02, 0.01, 0.03), "+xy", "M")
    doc.block("Half out", (0.09, 0.0, 0.0), (0.02, 0.02, 0.02), "M")
    doc.block("Gone", (0.2, 0.2, 0.2), (0.01, 0.01, 0.01), "M")
    r = _build(doc)
    assert _vol(r, "Ell") == pytest.approx(math.pi * 0.01 * 0.005 * 0.03, rel=1e-6)
    assert _vol(r, "Half_out") == pytest.approx(0.01 * 0.02 * 0.02, rel=1e-9)
    assert "Gone" not in r.region_names
    assert any("outside the solution domain" in n for n in r.notes)


def test_a_part_written_a_hair_off_the_board_does_not_leave_a_sliver():
    doc = Ecxml("snap").domain((0, 0, 0), (0.05, 0.05, 0.02))
    doc.pcb("Board", (0.01, 0.01, 0.005), (0.03, 0.03, 0.0016), "+xy", "M")
    doc.block("Chip", (0.02, 0.02, 0.0066000001), (0.005, 0.005, 0.001), "M")   # 0.1 um above
    r = _build(doc)
    assert r.region_names == ("air", "Board", "Chip")
    assert _vol(r, "Chip") == pytest.approx(0.005 * 0.005 * 0.001, rel=1e-4)


def test_an_external_mcad_file_is_never_opened_and_is_named_as_left_out(tmp_path):
    target = tmp_path / "part.step"
    target.write_text("ISO-10303-21;")
    doc = tiny_board().mcad("Bracket", (0, 0, 0), str(target), "Alu", 1.0)
    r = _build(doc)
    row = r.sidecar["not_built"][0]
    assert row["name"] == "Bracket" and row["file_name"] == str(target)
    assert "Bracket" not in r.region_names
    assert any("not part of the upload" in n for n in r.notes)


def test_names_are_mesh_safe_and_unique():
    doc = Ecxml("names").domain((0, 0, 0), (0.1, 0.1, 0.1))
    doc.block("2R CTM", (0.0, 0.0, 0.0), (0.01, 0.01, 0.01), "M")
    doc.block("Air", (0.02, 0.0, 0.0), (0.01, 0.01, 0.01), "M")
    doc.block("u1", (0.04, 0.0, 0.0), (0.01, 0.01, 0.01), "M")
    doc.block("U1", (0.06, 0.0, 0.0), (0.01, 0.01, 0.01), "M")
    doc.block("Outer", (0.08, 0.0, 0.0), (0.01, 0.01, 0.01), "M")     # a name snappy keeps
    assert _build(doc).region_names == ("air", "p_2R_CTM", "Air_2", "u1", "U1_2", "Outer_patch")


def test_the_job_materialiser_refuses_a_broken_ecxml_as_the_users_to_fix(tmp_path):
    from meshpipeline.application.geometry_materializer import canonical_path
    from meshpipeline.contracts.geometry_source import GeometrySourceError
    from meshpipeline.errors import FailureClass

    src = _write(tmp_path, _doc(_block()).replace(b"<plane>", b"").replace(b"<x>0.01", b"<x>z"),
                 "source.ecxml")
    with pytest.raises(GeometrySourceError) as err:
        canonical_path(src)
    assert err.value.failure_class is FailureClass.USER_INPUT
    assert "ECXML" in str(err.value)


def test_the_intake_hears_the_same_region_names_without_building_anything():
    from meshpipeline.cad.ingest.ecxml_build import build, plan_region_names

    overlap = (Ecxml("overlap").domain((0, 0, 0), (0.1, 0.1, 0.1))
               .block("Hidden", (0.01, 0.01, 0.01), (0.01, 0.01, 0.01), "M")
               .block("Cover", (0.0, 0.0, 0.0), (0.05, 0.05, 0.05), "M")
               .block("Away", (0.3, 0.3, 0.3), (0.01, 0.01, 0.01), "M"))
    sealed = (Ecxml("sealed").domain((-0.01, -0.01, -0.01), (0.07, 0.07, 0.07))
              .enclosure("Box", (0, 0, 0), (0.05, 0.05, 0.05), "M", 0.002))
    vented = (Ecxml("vented").domain((-0.01, -0.01, -0.01), (0.07, 0.07, 0.07))
              .enclosure("Box", (0, 0, 0), (0.05, 0.05, 0.05), "M", 0.002)
              .grille("Vent", (0.0, 0.01, 0.01), (0.002, 0.02, 0.02), "+yz", 1.0, 0.5))
    full = Ecxml("full").domain((0, 0, 0), (0.1, 0.1, 0.1)).block("All", (0, 0, 0), (0.1, 0.1, 0.1), "M")
    for doc in (set_top_box(), ducted_board(), tiny_board(), overlap, sealed, vented, full):
        model = read_ecxml(doc.xml())
        assert plan_region_names(model) == build(model).region_names


def test_every_watt_is_accounted_for():
    doc = Ecxml("power").domain((0, 0, 0), (0.1, 0.1, 0.1))
    doc.block("Kept", (0.0, 0.0, 0.0), (0.02, 0.02, 0.02), "M", 2.0)
    doc.block("Away", (0.3, 0.3, 0.3), (0.01, 0.01, 0.01), "M", 3.0)
    doc.source("Src", (0.05, 0.05, 0.05), (0.01, 0.01, 0.01), 4.0)
    doc.mcad("Ext", (0, 0, 0), "x.step", "M", 5.0)
    side = _build(doc).sidecar
    assert side["total_power_W"] == pytest.approx(14.0)
    assert side["power_W"] == {"in_solid_regions": 2.0, "in_heat_sources": 4.0,
                               "in_plates_not_meshed": 0.0, "in_parts_not_built": 8.0}
    assert {r["name"] for r in side["not_built"]} == {"Away", "Ext"}


def test_heatsink_blocks_count_toward_the_object_bound(monkeypatch):
    monkeypatch.setattr(ecxml_mod, "MAX_OBJECTS", 5)
    fins = [(f"F{i}", (0.001 * i, 0, 0), (0.0005, 0.01, 0.01), "M", 0.0) for i in range(10)]
    with pytest.raises(EcxmlError, match="more than 5 objects"):
        read_ecxml(Ecxml("hs").heatsink("HS", fins).xml())

# ------------------------------------------------------------------------------ fidelity ----
def _die_on_substrate(gap_m: float, *, scale: float = 1.0) -> Ecxml:
    """A 2 x 2 x 0.3 mm die over a 6 x 6 x 0.5 mm substrate, `gap_m` above it; `scale` grows the
    domain (and so the size the file's precision is judged at) without moving the parts."""
    doc = Ecxml("die").domain((0, 0, 0), (0.01 * scale, 0.01 * scale, 0.004 * scale))
    doc.material("Si", 2330, 700, 0.8, ("isotropic", 150.0))
    doc.material("Cu", 8900, 385, 0.1, ("isotropic", 390.0))
    doc.block("Substrate", (0.002, 0.002, 0.001), (0.006, 0.006, 0.0005), "Cu")
    doc.block("Die", (0.004, 0.004, 0.0015 + gap_m), (0.002, 0.002, 0.0003), "Si", 1.0)
    return doc


def _distance_mm(built, a: str, b: str) -> float:
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    shapes = dict(built.named)
    d = BRepExtrema_DistShapeShape(shapes[a], shapes[b])
    d.Perform()
    return float(d.Value())


def test_a_die_two_micrometres_above_its_substrate_is_never_fused_into_contact():
    r = _build(_die_on_substrate(2e-6))
    side = r.sidecar
    # independently of the build's own checks: the solids are still 2 um apart...
    assert _distance_mm(r, "Die", "Substrate") == pytest.approx(0.002, abs=1e-9)
    # ...no face is shared between them, and the air fills exactly what they leave
    assert ["Die", "Substrate"] not in side["solid_contacts"]
    assert _vol(r, "Die") == pytest.approx(0.002 * 0.002 * 0.0003, rel=1e-9)
    assert _vol(r, "air") == pytest.approx(0.01 * 0.01 * 0.004 - 0.006 * 0.006 * 0.0005
                                           - 0.002 * 0.002 * 0.0003, rel=1e-9)
    tol = side["tolerances_m"]
    assert tol["smallest_gap"] == pytest.approx(2e-6, rel=1e-6)
    assert tol["boolean_fuzzy"] <= 2e-7                 # a tenth of the gap at most
    assert any(line.startswith("Smallest gap between parts: 2 um") for line in r.report)
    assert any(line.startswith("Checked:") for line in r.notes)


def test_a_die_on_its_substrate_shares_the_face_between_them():
    r = _build(_die_on_substrate(0.0))
    assert ["Die", "Substrate"] in r.sidecar["solid_contacts"]
    assert _distance_mm(r, "Die", "Substrate") == pytest.approx(0.0, abs=1e-12)
    assert _vol(r, "Die") == pytest.approx(0.002 * 0.002 * 0.0003, rel=1e-9)


def test_a_gap_the_files_precision_cannot_tell_from_contact_is_refused_with_the_way_on():
    from meshpipeline.cad.ingest.ecxml_build import FidelityError

    # a 0.3 um bond line in a 1.3 m model: float32 cannot place it to better than ~0.1 um
    with pytest.raises(FidelityError) as err:
        _build(_die_on_substrate(3e-7, scale=100.0))
    said = str(err.value)
    assert "cannot be told apart from contact" in said and "0.3 um (Substrate and Die)" in said
    assert "sub-assembly" in said


def test_float32_noise_is_read_as_the_contact_it_is():
    # Flotherm's float32: the die written 10 nm above the substrate's top in a 0.3 m model
    r = _build(_die_on_substrate(1e-8, scale=30.0))
    assert ["Die", "Substrate"] in r.sidecar["solid_contacts"]
    assert r.sidecar["tolerances_m"]["coordinates_snapped"] >= 1
    assert any("read as the same plane" in line for line in r.report)


def test_every_overlap_and_drop_is_in_the_build_report():
    doc = Ecxml("report").domain((0, 0, 0), (0.1, 0.1, 0.1))
    doc.block("First", (0.0, 0.0, 0.0), (0.04, 0.04, 0.04), "M")
    doc.block("Second", (0.02, 0.0, 0.0), (0.04, 0.04, 0.04), "M")
    doc.block("Off", (0.07, 0.07, 0.07), (0.01, 0.01, 0.01), "M", active=False)
    doc.block("Away", (0.3, 0.3, 0.3), (0.01, 0.01, 0.01), "M")
    doc.plate("Sheet", (0.06, 0.07, 0.01), (0.03, 0.0, 0.03), "+xz", "M")
    r = _build(doc)
    text = "\n".join(r.report)
    assert "Overlap: Second overwrite(s) 32,000 mm3 of First (50% of it; the later object" in text
    assert "Switched off in the file, so not meshed: Off." in text
    assert "Not built: Away - it lies outside the solution domain." in text
    assert "Not meshed: Sheet, a plate without a usable thickness" in text
    assert r.report[-1].startswith("Checked: 2 of 2 solid volume(s) match the file's numbers")
    assert r.sidecar["build_report"] == r.report
    # what the user reads on the geometry check: the checks, the overlaps and every drop
    assert {n.split(":")[0] for n in r.notes} >= {"Checked", "Overlap", "Not built", "Not meshed"}


def test_a_build_that_lost_volume_is_refused(monkeypatch):
    from meshpipeline.cad.ingest import ecxml_build as eb

    real = eb._expected_volume
    monkeypatch.setattr(eb, "_expected_volume", lambda p, d: (real(p, d) or 0.0) * 1.01)
    with pytest.raises(eb.FidelityError, match="the file's numbers give"):
        _build(tiny_board())

# ------------------------------------------------------------------------------ the check ----
def test_the_check_takes_the_files_word_for_each_side_of_the_domain():
    from meshpipeline.application.geometry_check import declared_openings
    from meshpipeline.cad.ingest.ecxml_build import build

    side = build(read_ecxml(ducted_board().xml())).sidecar
    # whatever the shape-reading made of the air box (here: nothing, as for an external flow)
    facts = {"scale_to_m": 1.0, "notes": [], "openings": []}
    declared_openings(facts, side)
    got = {o["name"]: (o["role"], o["normal"], round(o["area_m2"], 9)) for o in facts["openings"]}
    # the fan blows in, the grille lets the air out, the four thin walls close their sides
    assert got == {"Inlet_fan": ("inlet", [0.0, 1.0, 0.0], 0.00432),
                   "Outlet": ("outlet", [0.0, -1.0, 0.0], 0.00432)}
    assert [o["id"] for o in facts["openings"]] == [1, 2]
    fan = next(o for o in facts["openings"] if o["name"] == "Inlet_fan")
    assert fan["centroid_mm"] == pytest.approx([0.0, 120.0, 12.0])
    assert any("closes the domain's -x, +x, -z, +z" in n for n in facts["notes"])
    # a model in open air: every side is an opening named for it
    facts = {"scale_to_m": 1.0, "notes": [], "openings": [{"id": 9}]}
    declared_openings(facts, build(read_ecxml(tiny_board().xml())).sidecar)
    assert [o["name"] for o in facts["openings"]] == [
        "domain_xmin", "domain_xmax", "domain_ymin", "domain_ymax", "domain_zmin", "domain_zmax"]


def test_a_fan_in_a_walled_side_is_offered_at_its_own_rectangle():
    from meshpipeline.application.geometry_check import declared_openings
    from meshpipeline.cad.ingest.ecxml_build import build

    doc = Ecxml("end wall").domain((0, 0, 0), (0.1, 0.2, 0.05))
    doc.plate("End", (0.0, 0.2, 0.0), (0.1, 0.001, 0.05), "+xz", "M")
    doc.fan2d("Pusher", (0.03, 0.2, 0.01), (0.04, 0.0, 0.03), "+xz", 0.002)
    doc.block("B", (0.02, 0.05, 0.01), (0.02, 0.02, 0.02), "M")
    facts = {"scale_to_m": 1.0, "notes": [], "openings": []}
    declared_openings(facts, build(read_ecxml(doc.xml())).sidecar)
    pusher = next(o for o in facts["openings"] if o["name"] == "Pusher")
    assert pusher["role"] == "outlet" and pusher["area_m2"] == pytest.approx(0.04 * 0.03)
    assert pusher["centroid_m"] == pytest.approx([0.05, 0.2, 0.025])
    # the bare sides stay open; with the fan blowing out, they are where the air comes in
    assert {o["role"] for o in facts["openings"] if o["name"].startswith("domain_")} == {"inlet"}


def test_a_partly_walled_side_offers_only_its_open_part():
    from meshpipeline.application.geometry_check import declared_openings
    from meshpipeline.cad.ingest.ecxml_build import build

    doc = Ecxml("half").domain((0, 0, 0), (0.1, 0.2, 0.05))
    doc.plate("Lower", (0.0, 0.2, 0.0), (0.1, 0.001, 0.03), "+xz", "M")   # 60% of the +y side
    doc.block("B", (0.02, 0.05, 0.01), (0.02, 0.02, 0.02), "M")
    facts = {"scale_to_m": 1.0, "notes": [], "openings": []}
    declared_openings(facts, build(read_ecxml(doc.xml())).sidecar)
    side = next(o for o in facts["openings"] if o["name"] == "domain_ymax")
    assert side["area_m2"] == pytest.approx(0.1 * 0.02)
    assert side["centroid_m"] == pytest.approx([0.05, 0.2, 0.04])
    assert (side["width_mm"], side["height_mm"]) == pytest.approx((100.0, 20.0))


def test_an_l_shaped_gap_is_offered_as_rectangles_that_hold_only_open_area():
    from meshpipeline.application.geometry_check import declared_openings
    from meshpipeline.cad.ingest.ecxml_build import build

    doc = Ecxml("corner").domain((0, 0, 0), (0.1, 0.2, 0.05))
    doc.plate("Corner", (0.0, 0.2, 0.0), (0.06, 0.001, 0.03), "+xz", "M")
    doc.block("B", (0.02, 0.05, 0.01), (0.02, 0.02, 0.02), "M")
    facts = {"scale_to_m": 1.0, "notes": [], "openings": []}
    declared_openings(facts, build(read_ecxml(doc.xml())).sidecar)
    gap = {o["name"]: o for o in facts["openings"] if o["name"].startswith("domain_ymax")}
    assert set(gap) == {"domain_ymax_1", "domain_ymax_2"}
    for o in gap.values():
        assert o["area_m2"] == pytest.approx(o["width_mm"] * o["height_mm"] * 1e-6)
    assert sum(o["area_m2"] for o in gap.values()) == pytest.approx(0.1 * 0.05 - 0.06 * 0.03)
    assert gap["domain_ymax_1"]["centroid_m"] == pytest.approx([0.05, 0.2, 0.04])

def test_a_rectangular_opening_reaches_the_patches_as_a_rectangle():
    from types import SimpleNamespace

    from meshpipeline.application.geometry_confirmation import patches_from

    fan = SimpleNamespace(name="Inlet_fan", role="inlet", diameter_mm=74.2, width_mm=120.0,
                          height_mm=36.0, centroid_mm=[0.0, 120.0, 12.0])
    round_port = SimpleNamespace(name="out", role="outlet", diameter_mm=50.0, width_mm=None,
                                 height_mm=None, centroid_mm=None)
    got = patches_from(SimpleNamespace(flow="internal", openings=[fan, round_port]))
    assert got[0] == {"name": "Inlet_fan", "type": "inlet", "width_mm": 120.0, "height_mm": 36.0,
                      "near_mm": [0.0, 120.0, 12.0]}
    assert got[1] == {"name": "out", "type": "outlet", "diameter_mm": 50.0}


def test_the_scout_measures_a_thermal_models_faces_without_searching_for_openings(stb):
    from meshpipeline.cad.scout import scout_cad
    from meshpipeline.contracts.coordinate_state import from_occ_transfer
    from meshpipeline.contracts.geometry_units import GeometryInterpretation, ResolutionBasis

    interp = GeometryInterpretation(
        interpretation_id="i", owner_id="o", geometry_source_id="s", unit=LengthUnit.millimetre,
        scale_to_metres=1e-3, basis=ResolutionBasis.file_declared, evidence="ecxml")
    result = scout_cad(stb["canonical"].path, declared_openings=True,
                       prepared=from_occ_transfer(interp, LengthUnit.millimetre))
    facts = result.as_dict()
    # the file states its openings (declared_openings, in the check); the flat faces are still
    # measured for the stage, in metres
    assert facts["openings"] == [] and facts["faces"]
    assert facts["solids"] == 9
    assert facts["bbox_max_m"] == pytest.approx([0.15, 0.12, 0.06], abs=1e-9)


# ------------------------------------------------------------------------------ size bound --
def _grid_board(doc: Ecxml, name: str, x0: float, y0: float, cols: int, rows: int) -> None:
    doc.pcb(name, (x0, y0, 0.002), (cols * 0.004, rows * 0.004, 0.0016), "+xy", "M")
    for r in range(rows):
        for c in range(cols):
            doc.block(f"{name} U{r * cols + c + 1}", (x0 + c * 0.004 + 0.001, y0 + r * 0.004 + 0.001,
                                                     0.0036), (0.002, 0.002, 0.001), "M", 0.1)


def test_the_size_bound_is_the_measured_cost_not_a_part_count(monkeypatch):
    from meshpipeline.cad.ingest import ecxml_build as eb

    # the cost model reproduces the timings it was fitted on, from above
    for n, measured_s in ((500, 28), (1000, 72), (2000, 276)):        # one board, N parts
        assert measured_s <= eb.predicted_cost(n, n * n)[0] <= 1.5 * measured_s
    assert 82 <= eb.predicted_cost(2020, 20 * 100 ** 2 + 2020)[0]     # 20 cards of 100
    assert 640 <= eb.predicted_cost(4040, 40 * 100 ** 2 + 4040)[0] <= 1.5 * 640   # 40 cards

    # the same ~3,000 parts: over the budget on one board, inside it on 30 cards
    one = Ecxml("one board").domain((0, 0, 0), (0.25, 0.25, 0.01))
    _grid_board(one, "Board", 0.01, 0.01, 55, 55)                    # 3,025 parts on one board
    with pytest.raises(eb.ModelTooLargeError) as err:
        _build(one)
    said = str(err.value)
    assert "3,026 solid parts (3,025 of them on one part, Board)" in said
    assert "Option B" in said and "sub-assembly" in said
    class Affordable(Exception):
        pass

    def stop(*_a, **_k):
        raise Affordable
    monkeypatch.setattr(eb, "_check_resolvable", stop)               # stop before the booleans
    many = Ecxml("many boards").domain((0, 0, 0), (0.25, 0.25, 0.01))
    for k in range(30):
        _grid_board(many, f"Card {k + 1}", 0.005 + (k % 6) * 0.04, 0.005 + (k // 6) * 0.045, 10, 10)
    with pytest.raises(Affordable):
        _build(many)                                                 # 3,030 parts: converted
