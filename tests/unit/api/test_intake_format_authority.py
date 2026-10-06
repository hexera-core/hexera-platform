# Responsibility: Verify one registry declares the accepted geometry formats, with no second list on either side.
from __future__ import annotations

import re
from pathlib import Path

import pytest

from meshpipeline.contracts.intake_formats import (
    ACCEPTED_SUFFIXES,
    INTAKE_FORMATS,
    capability_payload,
    declares_units,
    format_for_suffix,
    staged_name_for,
)

UI = Path(__file__).resolve().parents[3] / "ui"
SRC = Path(__file__).resolve().parents[3] / "src" / "meshpipeline"


# the registry itself

@pytest.mark.parametrize("suffix", [
    ".stl", ".step", ".stp", ".iges", ".igs", ".vtp", ".brep", ".brp", ".obj", ".ply", ".off",
    ".3mf", ".glb", ".gltf", ".vtk", ".vtu", ".msh", ".bdf", ".nas", ".inp", ".mesh", ".su2",
    ".3dm"])
def test_every_currently_implemented_format_is_declared(suffix):
    assert suffix in ACCEPTED_SUFFIXES
    assert format_for_suffix(suffix) is not None


@pytest.mark.parametrize("given,kind", [
    (".step", "cad"), (".STP", "cad"), ("iges", "cad"), (".brep", "cad"),
    ("/w/geometry/source.step", "cad"), ("C:\\parts\\Wing.IGS", "cad"),
    (".stl", "surface"), ("source.canonical.stl", "surface"), (".vtp", "surface"),
    (".obj", "surface"), (".glb", "surface"), (".msh", "surface"), (".3dm", "surface"),
    ("/tmp/a.b/part.inp", "surface"), (".sldprt", None), ("noext", None), ("", None)])
def test_one_helper_answers_cad_or_surface_for_a_path_or_a_suffix(given, kind):
    from meshpipeline.contracts.intake_formats import geometry_kind, is_cad, is_surface

    got = geometry_kind(given)
    assert (got.value if got else None) == kind
    assert is_cad(given) is (kind == "cad") and is_surface(given) is (kind == "surface")


def test_only_the_formats_read_as_they_are_are_canonical():
    canonical = {f.key for f in INTAKE_FORMATS if f.canonical}
    # today's formats: every engine reads these bytes exactly as it did before cad/ingest
    assert canonical == {"stl", "step", "iges", "vtp"}


def test_every_format_names_its_kind_in_the_capability_payload():
    kinds = {f["key"]: f["kind"] for f in capability_payload()["formats"]}
    assert {k for k, v in kinds.items() if v == "cad"} == {"step", "iges", "brep"}
    assert all(v in ("cad", "surface") for v in kinds.values())


def test_only_formats_that_record_a_unit_in_the_file_declare_one():
    by_key = {f.key: f.declares_units for f in INTAKE_FORMATS}
    assert {k for k, v in by_key.items() if v} == {"step", "iges", "3mf", "3dm"}
    # glTF's spec says metres but the file records nothing, and real files break the rule
    assert by_key["glb"] is False and by_key["gltf"] is False and by_key["brep"] is False


def test_vtp_is_declared_and_reachable():
    fmt = format_for_suffix(".vtp")
    assert fmt is not None and fmt.key == "vtp"
    assert ".vtp" in capability_payload()["accept"]


def test_capability_payload_describes_units_per_format():
    by_key = {f["key"]: f for f in capability_payload()["formats"]}
    # STEP and IGES carry a unit context that OpenCASCADE normalises on read
    assert by_key["step"]["declares_units"] is True
    assert by_key["iges"]["declares_units"] is True
    # STL and VTK PolyData are bare coordinates - the user must be asked
    assert by_key["stl"]["declares_units"] is False
    assert by_key["vtp"]["declares_units"] is False
    assert declares_units(".stl") is False and declares_units(".step") is True


@pytest.mark.parametrize("suffix,staged", [
    (".stl", "input.stl"), (".vtp", "input.vtp"), (".iges", "input.iges"),
    (".igs", "input.iges"), (".step", "input.step"), (".stp", "input.step"),
])
def test_staging_representation_comes_from_the_registry(suffix, staged):
    assert staged_name_for(suffix) == staged


# no second list anywhere

def test_the_server_holds_no_second_suffix_list():
    upload = (SRC / "api" / "v1" / "upload.py").read_text()
    assert "_ALLOWED_SUFFIXES" not in upload
    assert "ACCEPTED_SUFFIXES" in upload
    # and the rejection message is generated, not typed out again
    assert ".stl, .vtp, .step" not in upload


def test_the_browser_holds_no_second_suffix_list():
    composer = (UI / "js" / "shell" / "composer.js").read_text()
    index = (UI / "index.html").read_text()
    assert "ACCEPTED = [" not in composer, "the browser reintroduced its own allowlist"
    assert not re.search(r'accept="\.[a-z]', index), "index.html hardcoded an accept list"
    assert "loadIntakeFormats" in composer


def test_the_browser_never_rejects_when_capabilities_are_unavailable():
    src = (UI / "js" / "shell" / "intake_formats.js").read_text()
    assert "if (!intake) return true;" in src, (
        "isOfferable must permit selection when capability data is missing")


def test_public_copy_claims_nothing_the_product_does_not_do():
    blob = " ".join((UI / "js" / "shell" / p).read_text()
                    for p in ("composer.js", "intake_formats.js"))
    lowered = blob.lower()
    for overclaim in ("all cad", "any cad", "automatically convert", "auto-convert"):
        assert overclaim not in lowered, f"UI copy overclaims: {overclaim!r}"


def test_the_copy_distinguishes_acceptance_from_engine_compatibility():
    src = (UI / "js" / "shell" / "intake_formats.js").read_text()
    assert "Compatibility with a particular mesh engine is" in src


# server behaviour

@pytest.mark.parametrize("suffix", sorted(ACCEPTED_SUFFIXES))
def test_the_server_accepts_every_declared_suffix(suffix):
    from meshpipeline.contracts.intake_formats import unsupported_message
    assert suffix in ACCEPTED_SUFFIXES
    # the refusal text names the real set rather than a stale subset
    msg = unsupported_message(".xyz")
    assert suffix in msg


def test_an_undeclared_suffix_is_refused_with_the_real_list():
    from meshpipeline.contracts.intake_formats import unsupported_message
    msg = unsupported_message(".dwg")
    assert ".dwg" in msg
    for s in ACCEPTED_SUFFIXES:
        assert s in msg
    # and it still says what to do instead
    assert "export a STEP file and upload that" in msg


def test_a_file_without_an_extension_is_refused_in_words_not_empty_quotes():
    from meshpipeline.contracts.intake_formats import unsupported_message
    msg = unsupported_message("")
    assert "''" not in msg and "without an extension" in msg
    assert all(s in msg for s in ACCEPTED_SUFFIXES)


def test_the_registry_and_the_capability_response_cannot_drift():
    payload_suffixes = {s for f in capability_payload()["formats"] for s in f["suffixes"]}
    assert payload_suffixes == set(ACCEPTED_SUFFIXES)
    assert set(capability_payload()["accept"].split(",")) == set(ACCEPTED_SUFFIXES)
    assert len(capability_payload()["formats"]) == len(INTAKE_FORMATS)


# native CAD: refused, but with the way on

#: Every native suffix the refusal must recognise, the tool it must name, and a piece of the
#: export it must describe. Written out here, not read from the registry, so a suffix that falls
#: out of the registry fails rather than quietly getting the generic refusal.
NATIVE = {
    ".sldprt":     ("SolidWorks", "File > Save As > STEP AP214 (*.step)"),
    ".sldasm":     ("SolidWorks", "File > Save As > STEP AP214 (*.step)"),
    ".prt":        ("Creo or NX", "from Creo or NX"),
    ".asm":        ("Creo or Solid Edge", "from Creo or Solid Edge"),
    ".catpart":    ("CATIA", "File > Save As, type stp"),
    ".catproduct": ("CATIA", "File > Save As, type stp"),
    ".ipt":        ("Inventor", "File > Export > CAD Format"),
    ".iam":        ("Inventor", "File > Export > CAD Format"),
    ".x_t":        ("Parasolid", "reads Parasolid"),
    ".x_b":        ("Parasolid", "reads Parasolid"),
    ".f3d":        ("Fusion 360", "File > Export"),
    ".par":        ("Solid Edge", "File > Save As"),
    ".psm":        ("Solid Edge", "File > Save As"),
    ".jt":         ("JT", "the CAD tool the JT came from"),
    ".sat":        ("ACIS", "reads ACIS"),
    ".sab":        ("ACIS", "reads ACIS"),
    ".fcstd":      ("FreeCAD", "File > Export, type STEP"),
    ".scdoc":      ("SpaceClaim", "File > Save As, type STEP"),
}


def test_rhino_is_read_now_and_its_export_path_is_kept_for_a_model_without_meshes():
    from meshpipeline.contracts.intake_formats import RHINO_EXPORT, native_format_for_suffix
    # .3dm moved from "refused with an export hint" to accepted (cad/ingest reads its meshes);
    # the hint is what a .3dm with no stored meshes is refused with instead
    assert ".3dm" in ACCEPTED_SUFFIXES and native_format_for_suffix(".3dm") is None
    assert "File > Export Selected" in RHINO_EXPORT


@pytest.mark.parametrize("suffix", sorted(NATIVE))
def test_a_native_cad_file_is_told_which_tool_and_how_to_export_a_step(suffix):
    from meshpipeline.contracts.intake_formats import native_format_for_suffix, unsupported_message
    tool, export = NATIVE[suffix]
    fmt = native_format_for_suffix(suffix)
    assert fmt is not None and fmt.tool == tool
    msg = unsupported_message(suffix)
    assert msg.startswith(f"Hexera can't open {tool} files ({suffix}).")
    assert export in msg
    # why STEP, and that STL is the fallback that costs the units
    assert "STEP file: it keeps the exact surfaces and the units" in msg
    assert "STL also works, but loses the units" in msg
    # one or two sentences, no list of every accepted suffix to wade through
    assert msg.count(". ") <= 1 and msg.endswith(".")
    assert "Accepted geometry formats" not in msg and "Unsupported" not in msg
    # upper-case as a CAD tool writes it is the same file
    assert unsupported_message(suffix.upper()) == msg


def test_every_registered_native_suffix_is_one_the_test_names():
    from meshpipeline.contracts.intake_formats import NATIVE_CAD_SUFFIXES
    assert set(NATIVE_CAD_SUFFIXES) == set(NATIVE)


def test_no_native_suffix_is_also_an_accepted_one():
    from meshpipeline.contracts.intake_formats import NATIVE_CAD_SUFFIXES
    assert not NATIVE_CAD_SUFFIXES & ACCEPTED_SUFFIXES


@pytest.mark.parametrize("name,suffix", [
    ("bracket.sldprt", ".sldprt"), ("Wing.CATPart", ".catpart"), ("rotor.x_t", ".x_t"),
    # Creo numbers every save: the refusal must still see a Creo part, not a '.3' file
    ("bracket.prt.3", ".prt"), ("ASSY.ASM.12", ".asm"),
    ("model.step", ".step"), ("v1.2", ".2"), ("noext", ""),
])
def test_the_refusal_reads_the_suffix_a_user_would(name, suffix):
    from meshpipeline.contracts.intake_formats import refusal_suffix
    assert refusal_suffix(name) == suffix


def test_the_browser_is_sent_the_server_s_own_native_refusals_and_may_offer_them():
    from meshpipeline.contracts.intake_formats import unsupported_message
    payload = capability_payload()
    assert set(payload["native_cad"]) == set(NATIVE)
    for suffix, said in payload["native_cad"].items():
        assert said == unsupported_message(suffix)
    # the picker offers what is accepted AND what it can explain; `accept` keeps its meaning
    assert set(payload["picker_accept"].split(",")) == set(ACCEPTED_SUFFIXES) | set(NATIVE)
    assert set(payload["accept"].split(",")) == set(ACCEPTED_SUFFIXES)


@pytest.mark.parametrize("name,tool", [
    ("bracket.sldprt", "SolidWorks"), ("bracket.prt.3", "Creo or NX"), ("wing.CATPart", "CATIA"),
])
async def test_the_multipart_upload_refuses_native_cad_with_the_export_hint(name, tool):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from meshpipeline.api.v1.upload import router
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/upload")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/v1/upload/step-file",
                            files={"file": (name, b"native bytes", "application/octet-stream")})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail.startswith(f"Hexera can't open {tool} files") and "STEP" in detail


def test_the_browser_shows_the_native_refusal_and_offers_native_files():
    src = (UI / "js" / "shell" / "intake_formats.js").read_text()
    assert "intake.picker_accept || intake.accept" in src
    assert "intake.native_cad" in src and "export function refusalCopy" in src
    composer = (UI / "js" / "shell" / "composer.js").read_text()
    assert 'deps.notice.show(refusalCopy(file.name, intake), "error", { sticky: true })' in composer
