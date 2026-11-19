# CRS white paper — Overleaf source

Three documents, all self-contained:

| File | Length | For |
|---|---|---|
| `customer-brief.tex` | 5 pages | For a CAE engineer evaluating what CRS does. Shares the preamble, title block and table style of `main.tex` so the set reads as one family. Outcome-first, no methodology, no internal roadmap; every figure is still measured and traceable. |
| `main.tex` | 17 pages | The paper. Internal reading and clients who want the system, the guarantees and the numbers without the full treatment. |
| `full-paper.tex` | 23 pages | The long form: adds the methodology section in full, the Formula 1000 case study and a discussion of what the results mean. |

`main.tex` is a cut of the long version, not a different account: same claims, same numbers, no
discussion section and no case study. `../build_crs_technical_overview.py` renders the long version
through reportlab (25 pages). All three are maintained by hand; change the ones you need and say
which is canonical for that revision.

## The agentic loop section (CRS v3) — read this before editing it

`main.tex` §4, `full-paper.tex` §5 and the reportlab §5 all carry **The Agentic Loop**: goal
profiles, the cheap-oracle ladder with the big/XL meshers banned by an AST import blocklist, the
grind cost predictor, the investigator tool loop, the scratch clone, tournament repair (the typeset
`algorithm` environment), tiered review, the measured review objection and the corpus gym.

**Nothing in that section is measured.** Its source of truth is the build ledger at
`.superpowers/sdd/2026-11-20-crs-v3-master/progress.md` (20 of 31 tasks merged; no evidence pass
run; every posture flag default-off), the spec at
`docs/superpowers/specs/2026-11-20-crs-v3-agentic-design.md`, and the code under
`src/meshpipeline/cad/repair/`. The rules for editing it:

- The 4x meshes-after figure is a **target**, not a result. Tasks 19 and 29 have not run. Write
  "targets" and "designed to", never "achieves".
- The status table (`tab:v3status`) is the honest record of what is missing. Keep it current, and
  keep the two bold rows bold: the tournament winner is not committed on the real session, and
  tiered review is inert because nothing declares a deviation threshold.
- The figures that **are** honest, and may be cited: 100,000 parts probed; 9,956 failures (9.9%);
  0 surface-stage overlaps in 5,871; 390 surface-stage periodic faces; 227 open loops; the 168-file
  tiny-shell family; the deterministic catalogue's ~8–12% ceiling; 34 one-shot model proposals of
  which 0 survived; the coarse probe at 76/79 against delivered-file truth with a 3/79 reader-effect
  divergence.
- The limitations that must stay stated: native tool calling is untested against a real provider;
  tier 2b reads `unavailable` on the pipeline worker image; production is flag-off and unmeasured;
  auto-accept is inert until a threshold is plumbed.
- `customer-brief.tex` carries only the short **In Development** section, with no numbers and no
  capability claim, under the brief's standing rule. Do not move an unmeasured capability into the
  brief's measured sections.

## Using it on Overleaf

1. New Project → Upload Project, or drag `main.tex` (or `full-paper.tex`) into an existing project.
2. Menu → Compiler: **pdfLaTeX**. Main document: whichever `.tex` you want to build.
3. Recompile twice so the cross-references resolve.

No figure files are needed: the figures are TikZ, the long version's algorithm is `algpseudocode`,
and both bibliographies are `thebibliography` blocks. Each `.tex` builds on its own.

## Compiling locally

```bash
latexmk -pdf main.tex        # or full-paper.tex; run latexmk -c afterwards to clear aux files
```

Needs a TeX distribution. On macOS: `brew install texlive` (no sudo, full TeX Live) or
`brew install --cask basictex` (small, needs sudo, then `tlmgr install` the packages below).

## Packages used

`charter` and `beramono` (text and code faces), `geometry`, `microtype`, `booktabs`, `tabularx`,
`tikz` (`arrows.meta`, `positioning`, `calc`), `caption`, `enumitem`, `titlesec`, `fancyhdr`,
`seqsplit`, `multicol`, `hyperref`, plus `algorithm` + `algpseudocode` in the long version. All are
in Overleaf's TeX Live.

## Conventions worth keeping

- `\code{...}` is inline mono; `\lcode{...}` is the same but breakable, for long paths and
  identifiers that would otherwise overrun a column.
- `customer-brief.tex` is written to sell, under one hard rule stated in its header: omit internal
  detail freely, never imply a capability we cannot demonstrate on the customer's own parts. They
  run it within the week, and anything overstated becomes the objection that kills the deal.
- Deliberately NOT in the brief, by the owner's decision: the numeric cap table, the deployment
  posture detail, the mesh-failure corpus statistics and frontier measurements, audience-profile
  copy, and any discussion of competing healing tools. Those live in `main.tex` and `full-paper.tex`, which is what the buyer's engineers get.
- `main.tex` leads with the Overview: problem, what the system does, the four conditions an edit
  must pass, and what it refuses to do. A reader who stops after page 1 still has the argument.
- House style is block paragraphs: no first-line indent, space between paragraphs, set once in the
  preamble. Do not add `\indent` or `\noindent` anywhere, and do not start a paragraph with a bare
  `\textbf{...}` lead-in — use `\topic{...}`, so every run-in lead-in spaces identically.
- Lists take their spacing from the `\setlist` defaults only. Per-list overrides are what made the
  indentation look inconsistent before, and `labelindent + labelwidth + labelsep` must keep summing
  to `leftmargin` or wrapped lines stop hanging under their item.
- Avoid the stock LLM cadences when editing: `X, not Y`; `not X, but Y`; a pair of short balanced
  clauses split by a semicolon or em dash; `Importantly`/`Notably` openers. Say the thing once,
  plainly. Both papers were swept for these.
- Dense tables call `\tablecode` after `\footnotesize`, which shrinks the mono face so kernel
  identifiers fit their column. Without it Table 2 overfulls.
- Every number in the paper comes from a committed evidence README under
  `devtools/validation/evidence/` or from a design spec in `docs/superpowers/specs/`. Update the
  paper with the evidence, never ahead of it. A whitepaper that overclaims is worse than one that
  is out of date.
