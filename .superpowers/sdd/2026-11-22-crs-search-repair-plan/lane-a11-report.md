# Lane A11 step 1: why gmsh_default and gmsh_alt still fail on VALID parts

DIAGNOSIS ONLY, nothing built. Measured on `jig` (one container at a time), native, from v2's own rows plus direct
measurement of the parts. Raw data: `lane-a-work/a11/{out.jsonl,tagfaces.json,tagmap.json,table.md}`.

## What I could diagnose, and what I could not

v2 dev rows available so far: 99 (26 shards, all still streaming). 40 have a VALID output; **32 of those fail one of
my members** (`gmsh_default` / `gmsh_alt`) on `overlapping_facets`, `edge_through_face` or `low_quality`. By arm:
input_only 14, staged 10, search_det 4, search_ai 4.

I diagnosed the **14 input_only files**, where the output IS the input and the geometry is therefore available. For
the other 18 rows the OUTPUT geometry does not exist anywhere I can reach: neither the v2 nor the v1r prefix has a
`work-states/` directory, so a repaired state cannot be measured without re-running the arm. Those 18 are counted,
not diagnosed.

**Two gaps worth fixing before step 2** (both cheap, both mine if you want them):
1. **C5's `located` never reaches the rows.** `arms.verdict_triage` copies a member's name, status, error_kind and
   cause - not `located`. The panel computes the locations; no row carries them. I recovered them by re-running the
   two members myself; a wave cannot.
2. **`work-states/` is not kept**, so only `input_only` rows are diagnosable after the fact.

## Where the failures are, by kind (this is the part that decides step 2)

| kind | rows | located? | what the location is |
|---|---|---|---|
| `low_quality` | 23 | **23 / 23** | an xyz POINT (the worst element), which I mapped to a face |
| `overlapping_facets` | 12 | **12 / 12** | a gmsh SURFACE TAG, no point ("on surface 24 surface 29") |
| `edge_through_face` | 4 | **0 / 4** | nothing. gmsh prints the point in its text ("PLC Error: A segment and a facet intersect at point") and the member does not parse it |

**A gmsh surface tag IS the OCCT face index. Measured, not assumed:** for all 14 parts I loaded the STEP in gmsh
exactly as the member does and compared every surface tag's bounding box with the OCCT face of the same index -
74/74, 477/477, 598/598, 843/843 ... **every face matched on every part, unit scale 1.0, no permutation**
(`tagmap.json`). So `overlapping_facets` can be aimed at a real face today, with no new plumbing.
`edge_through_face` cannot be aimed at all until somebody parses that point out of the message.

## The measured causes

Frequency over the 35 located rows (a row can carry more than one):

| measured cause | rows |
|---|---|
| touches a non-adjacent face (gap exactly 0) | 18 |
| sliver (aspect >= 20:1) | 10 |
| narrow (aspect 8-20:1) | 11 |
| near-tangent (gap under 1 mm, non-zero) | 10 |
| sharp dihedral (< 30 deg) | 7 |
| seam face | 5 |
| tiny edge (< 1e-4 x diagonal) | 2 |
| nothing measured stands out | 1 |

Read in order, that says: **the dominant cause is not size, it is proximity.** 18 of 35 located failures sit on a
face that TOUCHES a face it does not share an edge with, and another 10 sit within a millimetre of one. Slivers and
narrow faces are the second family (21 rows), sharp angles third (7), and the seam shows up in 5 - including 4 of
the 12 `overlapping_facets` rows, where the overlapping facets are on a seam face.

Two specific findings in the data:
- `7e573047c1a6` faces **108 and 109 are coincident duplicates** (identical area 9032.0789 mm2 and aspect 230:1,
  1.9 mm from anything else). `overlapping_facets` naming both is literally two copies of one face.
- `4c1fc7d5579d` and `e63710eae340` are **the same part** (477 faces, diagonal 437.112 mm, identical measurements
  at face 9). The dev_v2 split carries it twice under two shas, so one defect is counted twice in any rate.

## The table

| file | part | member | kind | where | measured cause |
|---|---|---|---|---|---|
| `263d5e2d5bab` | 0053_00532482_58a41a786e328e0f | gmsh_alt | low_quality | face 9 @ point | sliver 2995:1 |
| `263d5e2d5bab` | 0053_00532482_58a41a786e328e0f | gmsh_alt | low_quality | face 5 @ point | sliver 46:1, near-tangent 0.11 mm |
| `263d5e2d5bab` | 0053_00532482_58a41a786e328e0f | gmsh_default | low_quality | face 5 @ point | sliver 46:1, near-tangent 0.11 mm |
| `4c1fc7d5579d` | 0053_00539073_58a66b7522115e0f | gmsh_alt | low_quality | face 99 @ point | sliver 21:1, sharp 5 deg, touches a non-adjacent face |
| `4c1fc7d5579d` | 0053_00539073_58a66b7522115e0f | gmsh_alt | low_quality | face 47 @ point | narrow 19:1, sharp 5 deg, touches a non-adjacent face |
| `4c1fc7d5579d` | 0053_00539073_58a66b7522115e0f | gmsh_alt | low_quality | face 100 @ point | narrow 12:1, touches a non-adjacent face |
| `4c1fc7d5579d` | 0053_00539073_58a66b7522115e0f | gmsh_default | overlapping_facets | face 9 (gmsh tag) | narrow 14:1, near-tangent 0.254 mm, seam |
| `5accc12fa47d` | 0053_00532577_58a4246ffb62730f | gmsh_alt | low_quality | face 70 @ point | sliver 21:1, touches a non-adjacent face |
| `5accc12fa47d` | 0053_00532577_58a4246ffb62730f | gmsh_alt | low_quality | face 50 @ point | narrow 11:1, near-tangent 0.944 mm |
| `5accc12fa47d` | 0053_00532577_58a4246ffb62730f | gmsh_alt | low_quality | face 125 @ point | narrow 18:1 |
| `5accc12fa47d` | 0053_00532577_58a4246ffb62730f | gmsh_default | overlapping_facets | face 1 (gmsh tag) | sharp 26 deg, touches a non-adjacent face, seam |
| `615e4cde0518` | 0051_00514513_589b5807a417220f | gmsh_default | edge_through_face | NOT LOCATED | no location in the member result |
| `67a0c04ed27a` | 0056_00561477_ca18416aea564660 | gmsh_default | edge_through_face | NOT LOCATED | no location in the member result |
| `695d74ca7fa6` | 0052_00527796_945276583aeb7d42 | gmsh_alt | low_quality | face 5 @ point | touches a non-adjacent face |
| `695d74ca7fa6` | 0052_00527796_945276583aeb7d42 | gmsh_default | overlapping_facets | face 24 (gmsh tag) | sliver 59:1, touches a non-adjacent face |
| `695d74ca7fa6` | 0052_00527796_945276583aeb7d42 | gmsh_default | overlapping_facets | face 29 (gmsh tag) | sliver 277:1, touches a non-adjacent face |
| `722c122c9b80` | 0053_00535069_bb0bc8e7914fdea8 | gmsh_alt | low_quality | face 256 @ point | touches a non-adjacent face |
| `722c122c9b80` | 0053_00535069_bb0bc8e7914fdea8 | gmsh_alt | low_quality | face 431 @ point | near-tangent 0.528 mm |
| `722c122c9b80` | 0053_00535069_bb0bc8e7914fdea8 | gmsh_alt | low_quality | face 216 @ point | near-tangent 0.381 mm, seam |
| `722c122c9b80` | 0053_00535069_bb0bc8e7914fdea8 | gmsh_default | overlapping_facets | face 81 (gmsh tag) | touches a non-adjacent face, seam |
| `7e573047c1a6` | 0051_00518226_7aa4de49968de523 | gmsh_alt | timeout | NOT LOCATED | no location in the member result |
| `7e573047c1a6` | 0051_00518226_7aa4de49968de523 | gmsh_default | overlapping_facets | face 108 (gmsh tag) | sliver 230:1 |
| `7e573047c1a6` | 0051_00518226_7aa4de49968de523 | gmsh_default | overlapping_facets | face 109 (gmsh tag) | sliver 230:1 |
| `bc860ab1a1e1` | 0052_00524677_ce40b542d96733a3 | gmsh_alt | no_volume | NOT LOCATED | no location in the member result |
| `bc860ab1a1e1` | 0052_00524677_ce40b542d96733a3 | gmsh_default | overlapping_facets | face 1 (gmsh tag) | sharp 14 deg |
| `bc860ab1a1e1` | 0052_00524677_ce40b542d96733a3 | gmsh_default | overlapping_facets | face 6 (gmsh tag) | nothing measured stands out |
| `cbe8c3abc11c` | 0052_00524943_fa9a21c8b070173b | gmsh_alt | low_quality | face 355 @ point | tiny edge 0.001 mm, near-tangent 0.5 mm |
| `cbe8c3abc11c` | 0052_00524943_fa9a21c8b070173b | gmsh_alt | low_quality | face 537 @ point | narrow 17:1 |
| `cbe8c3abc11c` | 0052_00524943_fa9a21c8b070173b | gmsh_default | edge_through_face | NOT LOCATED | no location in the member result |
| `d2cb9938a2d7` | 00542755_475d3d5812881065217cd | gmsh_default | low_quality | face 62 @ point | narrow 17:1, sharp 21 deg, near-tangent 0.659 mm |
| `d2cb9938a2d7` | 00542755_475d3d5812881065217cd | gmsh_default | low_quality | face 17 @ point | narrow 18:1, sharp 21 deg, near-tangent 0.659 mm |
| `de9ad40118a4` | 0056_00567964_2dc8697785b603eb | gmsh_alt | low_quality | face 47 @ point | sliver 6523:1, tiny edge 0.0023 mm, touches a non-adjacent face |
| `de9ad40118a4` | 0056_00567964_2dc8697785b603eb | gmsh_alt | low_quality | face 13 @ point | touches a non-adjacent face |
| `de9ad40118a4` | 0056_00567964_2dc8697785b603eb | gmsh_default | overlapping_facets | face 13 (gmsh tag) | touches a non-adjacent face |
| `de9ad40118a4` | 0056_00567964_2dc8697785b603eb | gmsh_default | overlapping_facets | face 14 (gmsh tag) | touches a non-adjacent face |
| `e63710eae340` | 0059_00595948_58bf7c0dacb57910 | gmsh_alt | low_quality | face 159 @ point | narrow 9:1, touches a non-adjacent face |
| `e63710eae340` | 0059_00595948_58bf7c0dacb57910 | gmsh_alt | low_quality | face 87 @ point | narrow 19:1, sharp 5 deg, touches a non-adjacent face |
| `e63710eae340` | 0059_00595948_58bf7c0dacb57910 | gmsh_default | overlapping_facets | face 9 (gmsh tag) | narrow 14:1, near-tangent 0.254 mm, seam |
| `eda22c2f6e7c` | 0053_00531292_58a364a1d8d7800f | gmsh_alt | low_quality | face 40 @ point | touches a non-adjacent face |
| `eda22c2f6e7c` | 0053_00531292_58a364a1d8d7800f | gmsh_alt | low_quality | face 48 @ point | touches a non-adjacent face |
| `eda22c2f6e7c` | 0053_00531292_58a364a1d8d7800f | gmsh_default | edge_through_face | NOT LOCATED | no location in the member result |

## What this implies for step 2 (NOT built, awaiting your go)

- **Aim at proximity first, not at size.** `remove_small` and `unify_same_domain` address the sliver/narrow family
  (21 rows), but the largest family is "two faces that touch without sharing an edge" (18 rows), which neither op
  removes. The op that matches that geometry is a split or a conformalise at the contact, or a defeature of the
  smaller of the two touching faces - aimed at the named face, never part-wide.
- **`overlapping_facets` is ready to target now**: tag == face index is measured, and 4 of its 12 rows are a seam
  face, 1 is a duplicate pair.
- **`edge_through_face` is not targetable yet** (4 rows): its point exists only in gmsh's message text.
- **Do not aim at `low_quality` by face alone.** Its location is the worst ELEMENT's point; on 263d5e2d5bab the same
  face 5 is named by both members while the sliver at face 9 is 2995:1. The point says where the mesh is bad, which
  is not always where the geometry is wrong.
