# Lane C22: the doomed cap needs an ORDER - measured on 32ca3e9a6e9a, and the ranking risk of fixing it

## THE GENERAL LESSON

The doomed cap (C16) only starts once a member FAILS. With a FIXED member order (`panel.DOOM_ORDER`: gmsh_default,
gmsh_prod, gmsh_alt, netgen_occ) the cap's value is LUCK - whether the file's fast failure happens to come before its
slow hang:

- **5accc12fa47d wins the lottery.** gmsh_prod and gmsh_default fail in under 2 s, first; the two hangs that follow
  get 60 s caps. Healer: 824 s, of which ~680 s is the two capped members on 7 stages.
- **32ca3e9a6e9a loses it.** Its input panel: gmsh_default TIMEOUT 840 s (300 s + the retry), netgen fails in 2.0 s,
  prod/alt capped. gmsh_default is FIRST in the fixed order and netgen LAST, so on EVERY stage gmsh_default hangs
  before anything has failed: no doom, no cap, 300 s + 600 s retry, ~10 min a stage. In the fast run the healer judged
  ONE state in 1,196 s before the file wall.

A cap whose benefit depends on the order of a fixed list is half a fix. The other half: order every verdict AFTER the
input by the INPUT's own evidence - members that failed at the input, fastest first; then the input's passes in
DOOM_ORDER; members that timed out or were capped at the input, LAST. The input verdict keeps DOOM_ORDER (it has no
evidence yet). The order goes in the panel config hash, for the cap's reason: it changes who gets capped.

## THE RANKING RISK, MEASURED

Input full panels of the 15 fast-loop dev files (re-run on the tip, fast loop image, 1.5 vCPU), joined with every
later state of those files in fast runs 1829, 1926 and 2007 (2,020 work BREPs mapped to their verdict-cache keys).

- 9 (file, member) pairs timed out or were capped at the input; 104 later states were judged by them.
- 2 pairs ever passed later: 695d74ca7fa6 netgen (0.6 s - survives any cap) and 4c1fc7d5579d netgen (8 states, 6 at
  60.1-62.4 s; capped at 49 s at its input - a borderline member, not a hang).
- The new order changes a member's treatment only where its input hang comes BEFORE the first fast failure in
  DOOM_ORDER. On these 15 files that is ONE member: gmsh_default on 32ca3e9a6e9a. Its later states are in no cache
  (every panel had a timeout); sampled on a VM it TIMED OUT again on staged states 4 and 5.
- **Passes lost: 0 observed.** The change is a no-op on 14 of the 15 files and saves the healer on the 15th.

Caveats: input panels re-run (timeouts are load-sensitive); caches exclude panels with a plain timeout; the eval never
set the file deadline (Lane A), so these waves ran members at the full 300 s with no wall clipping; 15 files.

Lane D builds it in `panel.py` (owner of the panel code).
