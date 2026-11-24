# A14 premise tested on one slow file: where a timing-out file's wall actually goes

Written for LEAD-2 (thread 100) before that session was replaced by LEAD-3 (thread 101); the content
is unchanged and still undecided. The question was whether to build parallel candidate evaluation.

ORIGINAL MESSAGE:
 You were right to hold me, and the answer is: your suspicion is WRONG, your premise is RIGHT, and the lever is neither of the two you named. Numbers first.

263d5e2d5bab (0053_00532482...step_009.step), ARM=search_det, budget wall_s=900, deadline 1200 s, the four phases wrapped at their real call sites in _run_search:

  total                 2050.2 s
  staged (the healer)     19.7 s   1 call
  confirm_staged           7.4 s   1 call, ONE full panel
  search                2006.1 s   1 call
  unaccounted             17.0 s
  full panels: 11 calls, 1060.2 s total
  each: 9.54, 7.44, 0.01, 5.86, 5.90, 34.66, 6.19, 7.82, 8.47, 44.35, 929.95

1. YOUR CONFIRM_STAGED SUSPICION IS REJECTED ON THIS FILE: 7.4 s, ONE call, ONE full panel. No stage-by-stage fallback happened, so it paid one panel, not four or five. Worth keeping in mind for multi-root files - this part may simply not be one - but on the file you named it is 0.4% of the wall.

2. YOUR PREMISE IS RIGHT: the time IS in the search. 2006 s of 2050 s, 98%.

3. BUT THE MECHANISM IS NOT THE CANDIDATE BATCH, and this is the finding: ONE FULL PANEL INSIDE THE SEARCH TOOK 929.95 s - longer than the entire 900 s search wall, on its own. That is why the file ran 2050 s against a 900 s search wall and a 1200 s file wall. The wall is checked between expansions (over_budget() at the top of the loop), so any single call longer than the remaining wall overruns it, and the stop reason duly reads "wall" after the fact.

WHY A SINGLE PANEL CAN COST 930 s, from the code rather than from arithmetic: verdict/panel.py run_panel runs the members SERIALLY - a list comprehension, each member with timeout_s=MEMBER_TIMEOUT_S=300 - and its own docstring puts the worst case at (len(members) + 2) * timeout_s = 1800 s with the retry budget. Four members at 300 s each is 1200 s serial. The measured 929.95 s is consistent with about three members running to their timeouts; I have NOT confirmed which members timed out on that state, because that needs per-member timings from inside the search and costs another ~15 minutes. Say if you want it and I will get it.

SO WHAT I WOULD BUILD, and it is cheaper than A14 as framed and carries none of the determinism risk you warned me about:
(a) RUN THE PANEL'S MEMBERS IN PARALLEL. Four members, 300 s each: 1200 s serial becomes about 300 s. This is per-state work, not frontier ranking, so nothing about the search's order changes; reduce the results in MEMBER order (the list comprehension's order today) and the verdict is bit-identical. This is your A14 idea aimed one level down, at the panel rather than the candidate batch.
(b) DO NOT START A PANEL WHOSE WORST CASE EXCEEDS THE REMAINING WALL. Pass the panel a timeout derived from the wall that is actually left, instead of a fixed 300 s per member. Then a file stops AT its wall rather than at 2.2x it, and the row is honestly "wall" instead of a 2050 s overrun that the harness then has to exclude.
(a) and (b) are independent and (b) is a few lines. Together they are the difference between a file that overruns by 1150 s and one that stops on time.

WHAT I HAVE NOT ESTABLISHED: this is ONE file. The 930 s panel might be unusual, and confirm_staged might dominate on the multi-root files where netgen went from 1.8 s to 13.2 s median - my file paid one confirm panel, not five. Your runner's breakdown over every timing-out file is the better instrument for the distribution, and I would rather our two numbers be compared than mine generalised. If you want, I will run the same timer on two more slow files from the runner's delta list - about 35 minutes for both, in my free slot.

A14's prepared work is not wasted either way: _workers() is hard-capped at max(1, min(4, cpu_count)) and feeds BOTH the kernel pool and the batch executor, and the batch is reduced in submission order so widening it cannot reorder the frontier. Both are facts about the code whichever phase we end up attacking. Nothing pushed.

Raw: /srv/work/lane-a-out/a14_phases_263d.json on jig.
