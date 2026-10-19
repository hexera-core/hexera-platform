# Responsibility: Prove an ECXML model's nanometre layers survive the read and the placement: the file's own digits set the precision (no fixed 10 nm floor, no fixed 0.1 um round-off reach), round-off never changes a part's thickness, and every thin layer is either meshed or carried as a thin-layer resistance by one general rule.
from __future__ import annotations

import pytest
from tests.unit.cad.ecxml_models import Ecxml

from meshpipeline.cad.ingest import ecxml_films as F
from meshpipeline.cad.ingest.ecxml import (
    number_is_noisy,
    print_limit,
    read_ecxml,
    written_places,
)
from meshpipeline.cad.ingest.ecxml_build import _precision, _um, length_words
from meshpipeline.cad.ingest.ecxml_place import place, roundoff_reach

THICK = (1e-6, 1e-7, 1e-8, 1e-9)
OFFSETS = (0.0, 0.05, 5.0)


def _materials(doc: Ecxml) -> Ecxml:
    doc.material("Si", 2330, 700, 0.8, ("isotropic", 150))
    doc.material("Cu", 8900, 385, 0.1, ("isotropic", 390))
    doc.material("Oxide", 2200, 730, 0.9, ("isotropic", 1.4))
    doc.material("Graphite", 2200, 700, 0.9, ("orthotropic", (1500, 1500, 5)))
    doc.material("Varies", 2200, 700, 0.9, ("linear", (1.4, 0.001, 300)))
    return doc


def _stack(t: float, off: float = 0.0, places: int | None = None, *, power: float = 0.0,
           film: str = "Oxide") -> Ecxml:
    """A 10 x 10 mm silicon base, a film of thickness t, a copper cap - no domain."""
    doc = _materials(Ecxml(f"Stack {t:g}", places=places))
    doc.block("Base", (off, off, off), (0.01, 0.01, 0.0005), "Si")
    doc.block("Film", (off, off, off + 0.0005), (0.01, 0.01, t), film, power)
    doc.block("Cap", (off, off, off + 0.0005 + t), (0.01, 0.01, 0.0005), "Cu", 10.0)
    return doc


def _place(doc: Ecxml, tmp_path, **kw):
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = tmp_path / "m.ecxml"
    src.write_bytes(doc.xml())
    return place(read_ecxml(src), **kw)


# ------------------------------------------------------------------------------ the digits ----
@pytest.mark.parametrize(("text", "form"), [
    ("0.00160000", (8, False, True)), ("0.0016", (4, False, False)), ("1.0", (1, False, False)),
    ("1.600000e-03", (6, True, True)), ("1e-08", None), ("5", None)])
def test_how_a_number_is_written_is_read_from_its_digits(text, form):
    assert written_places(text) == form


def test_a_fixed_format_writer_sets_the_print_limit_and_a_shortest_form_one_does_not():
    fixed = {(8, False, True): 30, (8, False, False): 70}
    assert print_limit(fixed, exponent=False) == pytest.approx(1e-8)
    shortest = {(4, False, False): 40, (3, False, False): 30, (17, False, False): 3}
    assert print_limit(shortest, exponent=False) == 0.0
    # a person typing "0.050" beside "0.0016": no fixed writer
    typed = {(3, False, True): 2, (4, False, False): 5, (9, False, False): 4}
    assert print_limit(typed, exponent=False) == 0.0
    sci = {(6, True, True): 50, (6, True, False): 40}
    assert print_limit(sci, exponent=True) == pytest.approx(1e-6)


def test_a_float64_printed_in_full_is_exact_and_a_float32_one_is_noise():
    assert not number_is_noisy("0.0019000500000000001")     # 1.9 mm + 50 nm, printed by repr
    assert number_is_noisy("0.30000001192092896")           # float32 0.3 printed in full
    assert number_is_noisy("0.002600499999999")             # thirteen digits: binary noise


@pytest.mark.parametrize("off", OFFSETS)
def test_an_exact_decimal_file_has_no_fixed_floor(tmp_path, off):
    model = read_ecxml(_write(tmp_path, _stack(1e-9, off)))
    prec = _precision(model, model.active_objects)
    assert prec.noise < 1e-12                     # float64 round-off at 5 m: ~4e-14 m
    assert roundoff_reach(prec) < 1e-12


def test_an_eight_place_file_keeps_its_precision_and_round_off_reach(tmp_path):
    model = read_ecxml(_write(tmp_path, _stack(1e-6, places=8)))
    prec = _precision(model, model.active_objects)
    assert model.print_unit_m == pytest.approx(1e-8)
    assert prec.noise == pytest.approx(5e-9)      # half a unit tells two printed planes apart
    assert roundoff_reach(prec) == pytest.approx(1e-7)   # #192's reach, for these files only


def _write(tmp_path, doc: Ecxml):
    src = tmp_path / "m.ecxml"
    src.write_bytes(doc.xml())
    return src


# ------------------------------------------------------------------------------ never lost ----
@pytest.mark.parametrize("t", THICK)
@pytest.mark.parametrize("off", OFFSETS)
@pytest.mark.parametrize("places", [None, 12])
def test_a_film_is_never_dropped_or_merged_at_any_offset(tmp_path, t, off, places):
    pm = _place(_stack(t, off, places), tmp_path)
    film = [p for p in pm.parts if p.objects[0].name == "Film"]
    rows = pm.records["thin_layers"]
    assert not any(p.dropped for p in pm.parts)
    assert not pm.roundoff
    if film:                                      # meshed: its own two faces, exactly apart
        b = film[0].box
        assert b.hi[2] - b.lo[2] == pytest.approx(t, rel=1e-6)
    else:                                         # a resistance of exactly its thickness
        (row,) = rows
        assert row["thickness_m"] == pytest.approx(t, rel=1e-6)
        assert row["R_m2K_W"] == pytest.approx(t / 1.4, rel=1e-6)


def test_round_off_never_changes_a_parts_thickness(tmp_path):
    """8 places: a 50 nm film beside a 30 nm one. #192 read the 20 nm step between their tops as
    round-off and made the 50 nm film 30 nm thick; a part's own thickness is never round-off."""
    doc = _materials(Ecxml("Two films", places=8))
    doc.domain((-0.001, -0.001, -0.001), (0.02, 0.008, 0.004))
    doc.block("Base", (0, 0, 0), (0.016, 0.006, 0.001), "Si")
    doc.block("Thick film", (0, 0, 0.001), (0.006, 0.006, 5e-8), "Oxide")
    doc.block("Thin film", (0.008, 0, 0.001), (0.006, 0.006, 3e-8), "Oxide")
    pm = _place(doc, tmp_path, layers=False)
    by = {p.objects[0].name: p.box for p in pm.parts}
    assert by["Thick film"].hi[2] - by["Thick film"].lo[2] == pytest.approx(5e-8, rel=1e-6)
    assert by["Thin film"].hi[2] - by["Thin film"].lo[2] == pytest.approx(3e-8, rel=1e-6)


def test_a_ten_nanometre_film_written_to_eight_places_is_kept(tmp_path):
    """One unit of the last digit is still two planes: the film's size says 0.00000001."""
    pm = _place(_stack(1e-8, places=8), tmp_path)
    assert not any(p.dropped for p in pm.parts)
    (row,) = pm.records["thin_layers"]
    assert row["thickness_m"] == pytest.approx(1e-8, rel=1e-6)


def test_exporter_round_off_between_different_objects_still_merges_in_an_eight_place_file(
        tmp_path):
    doc = _materials(Ecxml("Round-off", places=8))
    doc.domain((-0.001, -0.001, -0.001), (0.012, 0.008, 0.004))
    doc.block("Base", (0, 0, 0), (0.006, 0.006, 0.001), "Si")
    doc.block("Over", (0, 0, 0.00100003), (0.006, 0.006, 0.001), "Cu")     # 3 units
    pm = _place(doc, tmp_path)
    by = {p.objects[0].name: p.box for p in pm.parts}
    assert by["Base"].hi[2] == by["Over"].lo[2]
    assert any("Exporter round-off" in ln and "10 units of the last digit" in ln
               for ln in pm.report)


def test_the_same_step_in_a_file_that_states_it_exactly_is_kept(tmp_path):
    doc = _materials(Ecxml("Step", places=None))
    doc.domain((-0.001, -0.001, -0.001), (0.012, 0.008, 0.004))
    doc.block("Base", (0, 0, 0), (0.006, 0.006, 0.001), "Si")
    doc.block("Over", (0, 0, 0.00100003), (0.006, 0.006, 0.001), "Cu")
    pm = _place(doc, tmp_path)
    by = {p.objects[0].name: p.box for p in pm.parts}
    assert by["Over"].lo[2] - by["Base"].hi[2] == pytest.approx(3e-8, rel=1e-6)
    assert not pm.roundoff


# ------------------------------------------------------------------------------ the rule ------
def test_a_film_too_flat_to_mesh_and_thermally_thin_is_a_resistance(tmp_path):
    pm = _place(_stack(1e-8), tmp_path)
    assert [p.objects[0].name for p in pm.parts] == ["Base", "Cap"]
    (row,) = pm.records["thin_layers"]
    assert row["R_m2K_W"] == pytest.approx(1e-8 / 1.4)
    assert row["aspect_if_meshed"] > F.MAX_LAYER_ASPECT
    assert row["below"] == ["Base"] and row["above"] == ["Cap"]
    # copper conducts better than silicon: the cap grows down into the film's place
    by = {p.objects[0].name: p.box for p in pm.parts}
    assert by["Cap"].lo[2] == pytest.approx(0.0005) and by["Base"].hi[2] == pytest.approx(0.0005)
    assert row["grown_above_m"] == pytest.approx(1e-8) and row["grown_below_m"] == 0
    assert any(ln.startswith("Thin layers: 1 layer(s)") for ln in pm.report)


def test_a_film_with_sound_cells_is_meshed(tmp_path):
    pm = _place(_stack(1e-6), tmp_path)     # 0.5 um cells under 0.2 mm ones: aspect 400
    assert "Film" in [p.objects[0].name for p in pm.parts]
    assert not pm.records["thin_layers"]


def test_a_heated_film_a_heat_spreader_and_a_temperature_dependent_one_stay_meshed(tmp_path):
    heated = _place(_stack(1e-8, power=0.5), tmp_path / "a")
    # a 1 um graphite sheet between two 20 um oxide layers, 50 mm across: its cells would be
    # 2000 times wider than thick, but it carries most of the sideways heat
    doc = _materials(Ecxml("Spreader", places=None))
    doc.block("Below", (0, 0, 0), (0.05, 0.05, 2e-5), "Oxide")
    doc.block("Film", (0, 0, 2e-5), (0.05, 0.05, 1e-6), "Graphite")
    doc.block("Above", (0, 0, 2.1e-5), (0.05, 0.05, 2e-5), "Oxide", 1.0)
    spreader = _place(doc, tmp_path / "b")
    varies = _place(_stack(1e-8, film="Varies"), tmp_path / "c")
    for pm, why in ((heated, "gives off heat"), (spreader, "spreads heat sideways"),
                    (varies, "varies with temperature")):
        assert "Film" in [p.objects[0].name for p in pm.parts]
        (kept,) = pm.records["thin_layers_meshed"]
        assert why in kept["why"]


def test_every_layer_can_be_meshed_on_request(tmp_path):
    pm = _place(_stack(1e-9), tmp_path, layers=False)
    assert "Film" in [p.objects[0].name for p in pm.parts]
    assert not pm.records["thin_layers"]


def test_a_stack_of_films_collapses_onto_one_plane_and_their_resistances_add(tmp_path):
    doc = _materials(Ecxml("Two films stacked"))
    doc.block("Base", (0, 0, 0), (0.01, 0.01, 0.0005), "Si")
    doc.block("Barrier", (0, 0, 0.0005), (0.01, 0.01, 1e-8), "Oxide")
    doc.block("Seed", (0, 0, 0.0005 + 1e-8), (0.01, 0.01, 2e-8), "Oxide")
    doc.block("Cap", (0, 0, 0.0005 + 3e-8), (0.01, 0.01, 0.0005), "Cu", 10.0)
    pm = _place(doc, tmp_path)
    rows = pm.records["thin_layers"]
    assert sorted(r["name"] for r in rows) == ["Barrier", "Seed"]
    assert {r["plane_m"] for r in rows} == {0.0005}
    assert all(r["stack_thickness_m"] == pytest.approx(3e-8) for r in rows)
    from meshpipeline.cad.ingest.ecxml_films import applied_resistance

    own, applied = applied_resistance(rows, 150.0, 390.0, 0.0, 3e-8)
    assert own == pytest.approx(3e-8 / 1.4)
    assert applied == pytest.approx(3e-8 / 1.4 - 3e-8 / 390.0)


# ------------------------------------------------------------------------------ the words -----
@pytest.mark.parametrize(("m", "words"), [
    (1e-9, "1 nm"), (1e-8, "10 nm"), (5e-8, "50 nm"), (1e-6, "1 µm"), (2.5e-5, "25 µm"),
    (1.6e-3, "1.6 mm"), (0.0, "0 mm")])
def test_lengths_read_in_the_unit_that_fits(m, words):
    assert length_words(m) == words


def test_the_report_keeps_its_ascii_micrometres_and_says_nanometres():
    assert _um(2e-6) == "2 um" and _um(1e-8) == "10 nm"
