"""A relaxed iteration may accept a worse SHAPE. It may not accept a THINNER cell.

WHAT THIS FILE IS ABOUT. `addLayersControls` runs `nLayerIter` iterations and swaps
`meshQualityControls` for its `relaxed` sub-dictionary after `nRelaxedIter` of them - the layer
log says so in as many words, "Switched to relaxed meshQuality constraints." at iteration 3 of
10. Ours said

    relaxed { maxNonOrtho 75; }

one line, as the tutorials write it, so for seven of the ten iterations the only constraint named
was an angle.

THE MESH THAT CAME BACK IS THE EVIDENCE. The 155,303,668-cell plate of 2026-11-21 passed every
standard checkMesh test - aspect ratio 87.7976, non-orthogonality 64.2666, skewness 3.35087, face
flatness, cell volumes, face areas - and failed five extended ones on numbers BELOW THE FLOORS
THIS DICTIONARY ITSELF SETS:

    cell determinant      minimum 0           against minDeterminant 0.001   (3,146 cells)
    interpolation weight  minimum 0.0106797   against minFaceWeight  0.02    (4,390 faces)
    face volume ratio     minimum 0.00654685  against minVolRatio    0.01    (56 faces)
    smallest cell         3.74e-17 m3, a layer 0.059 um thick in a 25 um cell

Reproduced on the same part at 2 across in 124 s of layer pass: determinant 3.90492e-05 over
1,256 cells, interpolation weight 0.02062 over 1,452 faces, volume ratio 0.00911981, four failed
checks - the same signature at a sixty-fourth of the size.

SO BOTH BLOCKS COME FROM ONE PRODUCER NOW. `_quality_lines` is the only place a floor is written
and `RELAXED_SHAPE` is the only thing that may differ, which means a floor cannot be dropped from
one block by editing the other. That is how this got here: two hand-written blocks, and only one
of them was ever completed.
"""
import pytest

from meshpipeline.engines.cfmesh_conjugate import layers as L

#: The measured run's cell at the interface - what the stack is sized against. See
#: `test_a_layer_pass_is_judged_by_what_it_grew`, which reads the same run.
BL8_CELL_M = 25.1578e-06
#: The 20x20x10 blockMesh of the minVol ablation, where OpenFOAM's shipped 1e-13 extruded nothing.
ABLATION_CELL_M = 22.0035e-06
ABLATION_PYRAMID_M3 = (1.0 / 3.0) * (22.0035e-06 ** 2) * (11.0e-06)

INTERFACE = "fluid_to_solid"


def _geom(cell: float = BL8_CELL_M) -> dict:
    g = L.geometry_for(cell)
    g["n_layers"] = 3
    return g


def _dict_text(cell: float = BL8_CELL_M) -> str:
    return L.render_layers_dict(interface_patch=INTERFACE, interface_cell_m=cell, **_geom(cell))


def _blocks(text: str) -> tuple[dict, dict]:
    """(strict, relaxed) as {key: value}, read out of the rendered dictionary itself.

    Read from the TEXT and not from `_quality_lines`, because the thing being guarded is what
    OpenFOAM is handed, and a renderer that forgot to emit the relaxed block at all would satisfy
    any assertion made against the producer.
    """
    body = text[text.index("meshQualityControls"):]
    body = body[:body.index("\nwriteFlags")] if "\nwriteFlags" in body else body
    at = body.index("relaxed")
    # The relaxed block ENDS AT ITS OWN BRACE.  and  follow it
    # inside meshQualityControls, and taking everything to the end of the block put those two in
    # the relaxed set, where they are not in the strict one.
    strict_part, rest = body[:at], body[at:]
    close = rest.index("}") + 1
    relaxed_part = rest[:close]

    def kv(part: str) -> dict:
        out = {}
        for line in part.splitlines():
            line = line.strip().rstrip(";").strip()
            if not line or line in {"{", "}", "relaxed", "meshQualityControls"}:
                continue
            bits = line.split()
            if len(bits) == 2:
                out[bits[0]] = bits[1]
        return out

    return kv(strict_part), kv(relaxed_part)


# ------------------------------------------------------------------ the two blocks are one set

def test_the_relaxed_block_names_every_floor_the_strict_block_names():
    """THE WHOLE DEFECT IN ONE ASSERTION. OpenFOAM reads `relaxed` as a dictionary in its own
    right; a key it does not contain is not inherited from the block above it. One line of
    `maxNonOrtho` therefore left seven of ten iterations with no volume, determinant, weight,
    ratio or twist floor at all, and the mover's other repair is to thin."""
    strict, relaxed = _blocks(_dict_text())
    assert strict, "no strict quality block was rendered"
    assert relaxed, "no relaxed quality block was rendered"
    assert set(relaxed) == set(strict), (
        f"only in strict: {sorted(set(strict) - set(relaxed))}; "
        f"only in relaxed: {sorted(set(relaxed) - set(strict))}")
    # and it is not a one-liner any more
    assert len(relaxed) >= 12


def test_the_relaxed_block_loosens_the_shape_limits_and_nothing_else():
    """A relaxed iteration is the mover saying "I cannot fix this, let me accept a worse cell". A
    worse SHAPE costs the solver accuracy. A thinner cell has no floor of its own: thin goes to
    zero, and a cell of zero thickness is not a worse answer, it is not an answer."""
    strict, relaxed = _blocks(_dict_text())
    differ = {k for k in strict if strict[k] != relaxed[k]}
    assert differ == set(L.RELAXED_SHAPE), differ
    for key in ("minVol", "minTetQuality", "minDeterminant", "minFaceWeight",
                "minVolRatio", "minTwist"):
        assert relaxed[key] == strict[key], key


def test_every_loosened_limit_is_loosened_in_the_direction_that_permits_more():
    """`RELAXED_SHAPE` is four maxima, so relaxing one means raising it. A value that went the
    other way would be a tightening wearing the word relaxed."""
    strict, relaxed = _blocks(_dict_text())
    for key in L.RELAXED_SHAPE:
        assert key.startswith("max"), key
        assert float(relaxed[key]) > float(strict[key]), key


def test_a_floor_cannot_be_dropped_from_one_block_by_editing_the_other():
    """They are the same call with one flag, so there is nowhere for them to drift apart."""
    strict = dict(ln.split()[0:2] for ln in
                  (s.rstrip(";") for s in L._quality_lines(min_vol=1e-17)))
    relaxed = dict(ln.split()[0:2] for ln in
                   (s.rstrip(";") for s in L._quality_lines(min_vol=1e-17, relaxed=True)))
    assert set(strict) == set(relaxed)
    assert {k for k in strict if strict[k] != relaxed[k]} == set(L.RELAXED_SHAPE)


def test_the_meshquality_dict_and_the_layer_dict_agree_on_every_floor():
    """`checkMesh -meshQuality` reads `system/meshQualityDict` and the mover reads the block
    inside `snappyHexMeshDict`. Two files, one set of numbers, or the stage that builds the mesh
    and the stage that judges it are not talking about the same mesh."""
    strict, _ = _blocks(_dict_text())
    standalone = {}
    for line in L.render_mesh_quality_dict(BL8_CELL_M).splitlines():
        bits = line.strip().rstrip(";").split()
        if len(bits) == 2 and not bits[0].startswith("FoamFile"):
            standalone[bits[0]] = bits[1]
    for key, value in strict.items():
        assert standalone.get(key) == value, key


# ------------------------------------------------------------------ minVol scales with the mesh

def test_min_vol_scales_with_the_cell_because_it_is_an_absolute_volume():
    """It is a VOLUME, so it has to be derived from the mesh it judges. A flat constant is either
    a wall on a fine mesh - OpenFOAM's shipped 1e-13 extrudes nothing at 22 um - or no wall at
    all, which is what 1e-20 was."""
    assert L.min_vol_for(2 * BL8_CELL_M) == pytest.approx(8 * L.min_vol_for(BL8_CELL_M))
    assert L.min_vol_for(BL8_CELL_M) == pytest.approx(L.MIN_VOL_FRACTION * BL8_CELL_M ** 3)


def test_min_vol_sits_under_the_thinnest_legitimate_pyramid_and_over_a_hairline():
    """The thinnest cell a full stack may have is the first layer, 4.15 um in a 25.1578 um cell,
    whose face pyramid is (1/3)(25.1578e-6 x 4.15e-6)(25.1578e-6 / 2). The floor has to be clear
    of that and still catch the 0.059 um cell the plate came back with."""
    cell, first = BL8_CELL_M, L.FIRST_LAYER_FRACTION * BL8_CELL_M
    legit = (1.0 / 3.0) * (cell * first) * (cell / 2.0)
    hairline = (1.0 / 3.0) * (cell * 0.059e-06) * (cell / 2.0)
    floor = L.min_vol_for(cell)
    assert floor < legit / 10.0, (floor, legit)
    assert floor > hairline, (floor, hairline)


def test_the_ablation_mesh_would_still_grow_its_layers():
    """The measurement this constant exists for: a 22.0035 um blockMesh at OpenFOAM's shipped
    `minVol 1e-13` extruded 0 of 1600 faces, said "Mesh OK" and exited 0. A scaled floor is three
    orders of magnitude under that mesh's own face pyramid, so it is not that wall."""
    assert L.min_vol_for(ABLATION_CELL_M) < ABLATION_PYRAMID_M3 / 100.0
    assert L.min_vol_for(ABLATION_CELL_M) < 1e-13


def test_min_vol_refuses_a_cell_that_is_not_a_cell():
    for bad in (0.0, -1.0):
        with pytest.raises(L.LayerError):
            L.min_vol_for(bad)


def test_the_rendered_dict_carries_the_scaled_floor_and_not_a_constant():
    """The renderer takes the cell already - the fit guard needs it - so there is no reason for
    the floor inside the dictionary to be anything else."""
    text = _dict_text()
    assert "minVol" in text
    strict, relaxed = _blocks(text)
    assert float(strict["minVol"]) == pytest.approx(L.min_vol_for(BL8_CELL_M))
    assert float(relaxed["minVol"]) == pytest.approx(L.min_vol_for(BL8_CELL_M))
    # a different mesh gets a different floor, which a constant could never do
    other, _ = _blocks(_dict_text(ABLATION_CELL_M * 2))
    # NOT pytest.approx: its default absolute tolerance is 1e-12, which makes every pair of
    # numbers at this scale equal. The ratio is the thing being asserted anyway - a cell 1.749x
    # bigger is 5.35x the volume.
    assert float(other["minVol"]) / float(strict["minVol"]) == pytest.approx(
        ((ABLATION_CELL_M * 2) / BL8_CELL_M) ** 3, rel=1e-4)


# --------------------------------------------- a layer that cannot keep half its thickness goes

def test_the_thickness_floor_is_most_of_the_stack_and_not_a_fifth():
    """`minThickness` is the total stack thickness below which snappyHexMesh unextrudes the face
    instead of keeping what is left of it. It lives in `addLayersControls`, where no relaxed
    iteration can reach it, which is why it is the lever that worked when the dictionary was not.

    Swept on two_scale_plate_ported at 2 across against the fifth that shipped: cells under
    determinant 0.001 1,220 -> 28, faces under interpolation weight 0.05 1,306 -> 210, smallest
    cell 2.05349e-15 -> 4.31474e-15 m3, coverage 95.9350% -> 96.3032%, layer pass 123 s -> 69 s,
    and the mesher's own exit line goes from 2 illegal faces after ten iterations to "Finished
    meshing without any errors" after four.

    RE-TUNED TO 0.80 ALONGSIDE THE FEATURE ANGLE AND THE SMOOTHING, not reversed. 0.70 and 0.80
    were within noise of each other when the floor was the only thing moving - 0 illegal faces and
    0 volume-ratio faces at both, coverage 96.3032 against 96.3006. With the other two in, 0.80 is
    what clears the determinant cells: at 0.70 the mesh has 34 of them and fails 3 checks, at 0.80
    it has none and fails 2, with interpolation-weight faces 43 -> 8 and max aspect 21.41 -> 16.96.
    """
    assert L.MIN_THICKNESS_OF_STACK == 0.8
    got = L.geometry_for(BL8_CELL_M)
    stack = L.stack_thickness_m(first_layer_m=got["first_layer_m"],
                                expansion=got["expansion"], n_layers=got["n_layers"])
    assert got["min_thickness_m"] == pytest.approx(L.MIN_THICKNESS_OF_STACK * stack)
    # the fifth of a stack it used to be, on this cell, was 3.0 um
    assert got["min_thickness_m"] > 3.0e-06


def test_the_floor_still_clears_what_minfaceweight_implies():
    """`fit_complaint` refuses a floor that permits a layer thinner than `minFaceWeight 0.02`
    allows against a full-sized neighbour - the mover would make illegal faces by construction,
    which is what the delivered dict did at 0.400 um. Raising the floor can only move further
    from that bound, but the bound is what the guard is for, so it is asserted and not assumed."""
    got = L.geometry_for(BL8_CELL_M)
    assert got["min_thickness_m"] > 0.02 * BL8_CELL_M
    assert L.fit_complaint(interface_cell_m=BL8_CELL_M, **got) == ""


def test_the_floor_scales_with_the_stack_not_with_the_cell():
    """A floor that protects the stack has to be a fraction of the stack; as a fraction of the
    cell it would mean a different thing at every layer count."""
    a = L.geometry_for(BL8_CELL_M)
    b = L.geometry_for(2 * BL8_CELL_M)
    assert b["min_thickness_m"] == pytest.approx(2 * a["min_thickness_m"])
    stack_a = L.stack_thickness_m(first_layer_m=a["first_layer_m"], expansion=a["expansion"],
                                  n_layers=a["n_layers"])
    assert a["min_thickness_m"] / stack_a == pytest.approx(L.MIN_THICKNESS_OF_STACK)


def test_the_floor_is_half_of_the_stack_it_is_given_at_every_layer_count():
    """IT WAS HALF OF A THREE-LAYER STACK WHATEVER IT WAS TOLD. `geometry_for` sized the
    floor against N_SURFACE_LAYERS while the caller overwrote `n_layers` afterwards, so at
    one layer the
    floor came out at 7.5530 um against a 4.1500 um stack - 182% of it. Every face fails a bar it
    cannot reach, snappyHexMesh unextrudes all of them, prints no errors and exits 0 with no
    layers grown: the exact silent failure this module was written around, reintroduced by the fix
    for a different one."""
    for n in (1, 2, 3):
        g = L.geometry_for(BL8_CELL_M, n_layers=n)
        assert g["n_layers"] == n
        stack = L.stack_thickness_m(first_layer_m=g["first_layer_m"],
                                    expansion=g["expansion"], n_layers=n)
        assert g["min_thickness_m"] == pytest.approx(L.MIN_THICKNESS_OF_STACK * stack)
        assert g["min_thickness_m"] < stack
        assert L.fit_complaint(interface_cell_m=BL8_CELL_M, **g) == ""


def test_a_floor_the_stack_cannot_reach_is_refused():
    """The bound in the opposite direction from the `minFaceWeight` one, and the one that fails
    silently: above the stack, every face is unextruded and the run still exits 0."""
    bad = L.fit_complaint(interface_cell_m=BL8_CELL_M,
                          first_layer_m=BL8_CELL_M * L.FIRST_LAYER_FRACTION,
                          expansion=L.EXPANSION_RATIO, n_layers=1,
                          min_thickness_m=7.553e-06)
    assert "at or above the whole" in bad and "exit 0" in bad


def test_a_stack_with_no_layers_in_it_is_refused():
    with pytest.raises(L.LayerError):
        L.geometry_for(BL8_CELL_M, n_layers=0)


# ------------------------------------------------------- where a layer stops, it stops gradually

def test_a_layer_termination_is_spread_over_more_than_one_face():
    """`nBufferCellsNoExtrude 0` makes the mover stop extruding at exactly the face that failed,
    so a cell a fifth of a cell tall sits against a full one - a volume ratio of five and an
    interpolation weight at the floor. 1 stops it one cell earlier all round a termination.

    Measured on two_scale_plate_ported at 2 across, 4 ranks, 3 layers, 506,298 interface faces,
    against the 0 this shipped with: coverage 95.9121% -> 95.9350%, illegal faces 8 -> 2, concave
    cells 42,702 -> 38,597, faces under interpolation weight 0.05 1,452 -> 1,306, worst weight
    0.020620 -> 0.022364, worst determinant 3.90492e-05 -> 4.94893e-05, cells under determinant
    0.001 1,256 -> 1,220, layer pass 123.7 s -> 123.0 s. Better on every measure that moved, and
    not slower.
    """
    assert L.BUFFER_CELLS_NO_EXTRUDE == 1
    assert f"nBufferCellsNoExtrude       {L.BUFFER_CELLS_NO_EXTRUDE};" in _dict_text()


def test_the_repair_iterations_stop_after_the_first_relaxed_one():
    """FOUR, AND THE FOURTH IS THE FIRST RELAXED ONE.

    This used to assert ten, on a two-across plate's trajectory - 1608, 216, 136, 76, 74, 74,
    50, 52, 2, 2 - where stopping at three left 76 illegal faces instead of 2. That reading was
    about THREE, which is a different dictionary (see below), and it was taken on a rig.

    ON THE PART, job b8f09ae9 (279,294,127 cells), from its own snappyHexMesh.log:

        iteration   0        1       2       3       4       5       6       7       8       9
        illegal  372431   14094      79      48      38      30      26      26       9       8
        coverage 91.1145 90.7214 90.7198 90.7191 90.7186 90.7183 90.7180 90.7178 90.7175 90.7174

    Iterations 4 to 9 are six tenths of the pass. They take illegal faces 38 -> 8 and coverage
    DOWN 0.0017 of a point. 48 faces over 279 M cells is 0.17 per million against
    ILLEGAL_PER_MILLION_CEILING 200.0, the only ceiling this module judges the count by.

    THIS IS NOT A TRADE, because repair only ever removes layers: coverage falls monotonically
    from iteration 0 in all twelve variants of
    runs/layer_sweep-two_scale_plate_ported-2a-L3.json, so four finishes HIGHER than ten.

    AND IT IS NOT AN EXTRAPOLATION. `nLayerIter` is only the loop bound in snappyLayerDriver.C,
    the relaxed switch reads only `iteration` and `nRelaxedIter`, and the one `break` reads only
    `nTotChanged` - so iterations 0-3 above ARE the four-iteration run on the real part.
    """
    text = _dict_text()
    assert L.LAYER_ITER == 4
    assert L.N_RELAXED_ITER == 3
    assert f"nLayerIter                 {L.LAYER_ITER:>2};" in text
    assert f"nRelaxedIter                {L.N_RELAXED_ITER};" in text


def test_four_is_above_the_relaxed_count_so_the_relaxed_dictionary_is_still_read():
    """THREE IS NOT ONE LESS THAN FOUR HERE - IT IS A DIFFERENT DICTIONARY.

    snappyLayerDriver picks the quality bar with
    `iteration < nRelaxedIter ? motionDict : motionDict.subDict("relaxed")`. At nLayerIter 3 with
    nRelaxedIter 3 that ternary never selects the relaxed sub-dictionary, so RELAXED_SHAPE is
    still written to the file and never read, and OpenFOAM validates neither against the other
    and prints no warning. The clamp in `_layer_iter` refuses it for that reason, which is why
    the guard is `>` and not `>=`.
    """
    assert L.LAYER_ITER > L.N_RELAXED_ITER
    # the relaxed block is still rendered, so the one relaxed iteration has something to read
    assert "relaxed" in _dict_text()


def test_the_iteration_count_is_settable_without_a_deploy():
    """The one number in this dictionary that could not be measured on the real geometry without
    a code change, which is exactly what LAYER_FEATURE_ANGLE_ENV exists to avoid for the angle.
    Out of range or unreadable, the shipped value stands - and at or below nRelaxedIter is out of
    range, because it silently deletes the relaxed phase."""
    import os
    from importlib import reload

    def _with(value):
        old = os.environ.get(L.LAYER_ITER_ENV)
        os.environ[L.LAYER_ITER_ENV] = value
        try:
            return reload(L).LAYER_ITER
        finally:
            if old is None:
                os.environ.pop(L.LAYER_ITER_ENV, None)
            else:
                os.environ[L.LAYER_ITER_ENV] = old
            reload(L)

    assert _with("6") == 6
    assert _with("10") == 10
    assert _with("3") == L.LAYER_ITER_DEFAULT, "at nRelaxedIter the relaxed phase never runs"
    assert _with("0") == L.LAYER_ITER_DEFAULT
    assert _with("11") == L.LAYER_ITER_DEFAULT
    assert _with("banana") == L.LAYER_ITER_DEFAULT
    assert _with("1e999") == L.LAYER_ITER_DEFAULT


def test_the_case_guard_still_accepts_both_dictionaries():
    """`sandbox/foam_case_guard` refuses an active preprocessor directive, and it refuses again
    inside `foam_exec.check_mesh` - so a dictionary it dislikes throws away a finished mesh after
    every stage has succeeded. The relaxed block is more text, and more text is more chances."""
    from meshpipeline.sandbox import foam_case_guard as G

    assert not G._HASH_DIRECTIVE.search(_dict_text())
    assert not G._HASH_DIRECTIVE.search(L.render_mesh_quality_dict(BL8_CELL_M))
