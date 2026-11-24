# Lane A11 step 2: aiming ops at the measured mesher cause

Everything below was measured on the real kernel in the mesh image on `jig`; nothing is inferred from a unit test.
Where a claim has no measurement behind it, it says so.

## 1. The proximity family (priority 1) - MERGED as A11.2

Two defects, both measured:

- `sites_proximity` dropped exactly the pairs the meshers fail on. It skipped any pair sharing a VERTEX, which on a
  real part means every corner contact. It now skips only EDGE-sharing pairs (conformal neighbours) and reports the
  rest with `touching`, `area_a_mm2`, `area_b_mm2`. Contacts are present on 9 of the 14 dev parts, 3 to 81 pairs each.
- The det sweep built its proximity candidates from `itertools.combinations` of the site's faces. It now reads the
  site's own measured clashes; contacts route to `remove_duplicate_faces` and a `defeature` of the smaller face
  (D16 `located` source), never to `split_by_proximity`, whose plan is sized from the gap.

## 2. overlapping_facets (priority 2): what the failure actually names

8 located rows, all from `gmsh_default` (`gmsh_alt` never reported this kind on these parts). The message names two
surfaces; D16 resolves both. Two classes, and the split is clean:

| class | rows | what the boxes say |
|---|---|---|
| ONE surface named twice ("on surface 9 surface 9") | 4 | the face overlaps ITSELF |
| TWO surfaces named | 4 | boxes overlap in all 4; in 2 of them one face is ~2e-7 mm thick, lying on its neighbour |

Example of the pair class: `surface 108` and `surface 109`, both 2.5 x 5 x 1150 mm, boxes overlapping - two faces in
the same place. Example of the degenerate class: `surface 6` at 2e-07 x 40.0 x 82.5 mm against `surface 1`.

### What the ops do about it, applied on the real kernel and re-meshed

Every row below is an op applied in-process on the real pool, the result written back to STEP, and `gmsh_default`
re-run on it. 8 parts.

| op | applied | declined, and why the kernel said so | `overlapping_facets` after | layer 1 after |
|---|---|---|---|---|
| `split_angle` | 6 | 2: "found nothing to divide on the targets" | still failing on all 6 | valid on all 6 |
| `rebuild_face` | 3 | 4 as `no_change`: "the face is valid and has no self-intersection"; 1 `Standard_ConstructionError` | still failing on 2; the third turned into `periodic_surface` | the third LOST validity |
| `unify_same_domain` (aimed at the named pair) | 1 | 2 as `no_change`: it left the part as it was | still failing | valid |
| `remove_duplicate_faces` (aimed at the named pair) | 0 | 3 as `not_coincident` | - | - |

**No op in the library cleared this family on any of the 8 parts.** Three things that matters for:

1. `rebuild_face`'s premise is wrong here. It declines on 4 of the 8 because the named face is VALID and has no
   self-intersection - the overlap is between the mesh facets, not inside the face's wires. On the one part where it
   did apply, it broke layer 1.
2. `remove_duplicate_faces` declines `not_coincident` on all three pair cases: the two faces' boxes overlap, but the
   faces are not copies of each other. The degenerate case is a 2e-7 mm-thick face LYING ON its neighbour, which is
   not the same thing as a duplicate.
3. One part (`de9ad401`, the 2e-7 mm face) resolved NO located face at all - the member's fingerprint matched nothing
   in the workspace - so nothing could be aimed there even in principle. That is a D16 gap, not an op gap.

The aiming is still worth having: it stops `rebuild_face` and `split_angle` being sprayed across a 843-face part for
candidates that cannot help, and it puts the pair ops in front of the one pair the mesher named. But it does not
repair the family, and I am not claiming it does.

GAP IN THIS MEASUREMENT, stated rather than hidden: the run tried four ops and did NOT try `defeature` or
`remove_small`, and a 2e-7 mm-thick face lying on its neighbour is exactly what a removal is for. That measurement is
running next; until it lands, "no op repairs this family" is a statement about these four ops only.

## 3. The sliver and narrow family (priority 3): the measurement does NOT support the plan

The brief was `remove_small` / `unify_same_domain` "now that B12 removed their bounds". B12 removed the op-level
fidelity CAPS. The bound that decides whether `remove_small` can reach these faces is a different one and is still in
place: the proposal ladder's widths are fractions of `fidelity_bound(diag) = min(0.1 mm, 1e-3 x diagonal)`.

Measured over the 16 distinct faces a `low_quality` failure located:

- **2 of 16** are narrower than that cap, so `remove_small` can reach only those two.
- The rest are not slivers at all: in-plane short sides of 14 mm, 24 mm, 39 mm, 144 mm. Widening the cap far enough to
  reach them would be removing real geometry, not a sliver.
- **6 of 16** carry a tiny EDGE (min edge 0.001 to 0.2 mm, i.e. 1 to 4 times the 0.1 mm ceiling) on an otherwise
  ordinary face. Those are reachable only if the ladder stops having an absolute ceiling and stays
  diagonal-relative.
- 10 of 16 have neither a narrow side nor a tiny edge. Their signature is a small dihedral (4.5, 21, 46 degrees) with
  aspect 9 to 45 - a near-tangent wedge, which is the proximity family's shape, not the sliver family's.

This is the caveat I carried into step 2 coming true: `low_quality` locates the worst ELEMENT, so the named face is
not necessarily the defect. Counting "21 sliver/narrow rows" in step 1 counted parts that CONTAIN small faces, not
parts whose located face is small.

What I would do, stated as a proposal and not built: drop the absolute 0.1 mm ceiling from the det ladder (it is a
fidelity cap in the proposal, which the 2026-11-24 decision removes) and keep the rungs diagonal-relative, so the
6 tiny-edge cases become reachable while the verdict (valid + panel, with the gaming flag reporting the movement)
decides whether the edit is worth it. I have not changed it: it widens an edit on every part, which deserves an
explicit yes.
