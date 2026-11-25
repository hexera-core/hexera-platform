# Panel reproducibility, lane D, 2026-11-23

11 near-threshold files (10 nearest the 0.01 SICN floor in Task 43's records + Task 49's flipped file), every full-panel member, both forms the eval judges, 5 fresh processes each (`panel_repro.py run`, mesh image, `--cpus=1`, emulated amd64 on a host at load 144-355).

- `compare-5.json`: within each form, every member that COMPLETED gave a bit-identical result (status, min SICN, element count) in all 5 runs. One cell varied: netgen_occ on the original form of nist_ftc_11 finished in ~256 s in runs 1 and 5 (pass, min SICN 0.2873569032763322 both times) and timed out twice (~843 s, `netgen_timeout`) in runs 2-4, which started at the highest host load. That is a time stop, not a different mesh; see the handoff for the open question it raises.
- Between forms: the original STEP and the engine's re-export (`search.engine_form`) disagree on a member's pass/fail for 4 of 11 files, in both directions (gmsh_alt 0.00417 fail -> 0.0494 pass on Task 49's flipped file).
- `agree.jsonl` (`panel_repro.py agree`, after the fix): the re-probe row and the arms' input verdict are identical, member by member and in `clean`, on 11 of 11 files. 6 ran on the local mesh image, 5 on the jig context.
