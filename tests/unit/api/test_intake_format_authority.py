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

@pytest.mark.parametrize("suffix", [".stl", ".step", ".stp", ".iges", ".igs", ".vtp"])
def test_every_currently_implemented_format_is_declared(suffix):
    assert suffix in ACCEPTED_SUFFIXES
    assert format_for_suffix(suffix) is not None


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


def test_the_registry_and_the_capability_response_cannot_drift():
    payload_suffixes = {s for f in capability_payload()["formats"] for s in f["suffixes"]}
    assert payload_suffixes == set(ACCEPTED_SUFFIXES)
    assert set(capability_payload()["accept"].split(",")) == set(ACCEPTED_SUFFIXES)
    assert len(capability_payload()["formats"]) == len(INTAKE_FORMATS)
