# Where the test parts come from

The CAD authoring side. These scripts build the solids in `../../../cad/semiconductor/` from
parametric specs, verify each one against its own dimensions, and write STL and STEP.

They are here because they were under version control nowhere at all: the only copy lived in one
untracked folder, and the parts in `cad/semiconductor/` are useless the moment nobody can rebuild
them. Nothing in the mesh pipeline imports any of this.

## They do not run in the mesh image

They need OpenCASCADE through OCP, plus trimesh, and the mesh image has neither. They run on the
authoring machine:

    <python-with-OCP> devtools/semiconductor/geometry/murray_ported_export.py

Every one of them writes to `devtools/semiconductor/cad/`, a sibling of this folder, and that is
the MILLIMETRE STAGING AREA. It is not the corpus and it is not in git: a part becomes a test part
only once it has been scaled to metres and re-measured into `cad/semiconductor/`.

    devtools/semiconductor/run.sh import --from devtools/semiconductor/cad --scale 1e-3
    python devtools/semiconductor/index_parts.py

Two directories rather than one, because the unit is the thing that goes wrong. Millimetres in the
staging area and metres in the corpus means a part can only arrive through the step that scales it,
and `import` refuses anything whose longest side comes out over 0.3 m on the way through.

## Units are MILLIMETRES here

Everything in this folder is authored in millimetres, which is OpenCASCADE's convention and the
frame in which a 90 um feature is 0.09, comfortably inside the range where solid predicates behave.
The same shape built in metres fails outright: trimesh returns `is_volume False` for a cylinder of
radius 2e-4 because the end caps collapse.

An OpenFOAM case is in metres, so `import` scales by 1e-3 on the way in and refuses anything whose
longest side comes out over 0.3 m. A surface handed to a mesher still in millimetres does not fail,
it runs until somebody kills it.

## What is here

| file | what it does |
|---|---|
| `verify.py` | the checks every part has to pass: kernel validity, watertightness, volume, body count, smallest feature, tessellation convergence |
| `murray.py` | the Murray's-law branching tree, sealed inside the metal |
| `murray_ported.py` | the same tree with a feed and sixteen drains, so coolant can move through it |
| `murray_ported_export.py` | builds, verifies and exports that one |
| `published.py` | the parts taken from published dimension tables |
| `export_all.py` | builds, verifies and exports those |
| `advanced.py` | the unit cell, the hotspot array and the two-level manifold |
| `advanced_build.py`, `advanced_export.py` | builds, verifies and exports those |
| `catalogue15.py` | fifteen more designs, sized before being drawn, with what each would cost to mesh. Specs only: none of these is built yet |
| `specs_raw.json` | the extracted source dimensions the published parts were built from |

`catalogue15.py` is worth reading before adding anything: it reports what each design would cost at
the platform's own resolution and refuses the set if any part cannot be meshed on hardware that
exists. Two parts already in the package are faithful to their papers and need 1.43 billion and 9.3
billion cells, which is a test article nobody can mesh.
