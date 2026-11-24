## READ FIRST: this is the FIRST snapshot under the NEW goal (owner decision 2026-11-24)

- **Clean = layer 1 VALID + layer 2 full PANEL.** Faithfulness (layer 3) is still computed and reported, in the rows "clean but NOT faithful" below, but it NEVER gates clean. The geometry/fidelity caps are deleted (B12). Clean outputs that moved the part (|volume ratio − 1| > 0.25 or bbox ratio < 0.90) are FLAGGED as gaming, named per arm, and still counted clean.
- **Not comparable with v1 or v1r.** Those used the OLD clean (valid + panel + faithful). On this commit the deterministic arm also proposes new op rows: defeature (D14, ported from Raymond), fuse, `cut_by` at any depth, and `trim_unbounded` (C7). search_det and staged numbers measure a different generator, not v1's.
- **Splits:** `devtools/validation/corpora/v4/provisional_v2/PROVISIONAL_dev_v2.json` (150 failing) and `PROVISIONAL_clean_v2.json` (40 clean), at the run commit. These are PROVISIONAL, drawn from the partial reprobe-800 re-probe; the real splits come from the full re-probe.
- **Cache:** VERDICT_CODE_VERSION .4, a fresh prefix. Every verdict was meshed cold.
- **Image vs code:** the image's mesh build (toolchain) is `1d6ed735`; the run commit, bind-mounted, is `5c43220a`.
