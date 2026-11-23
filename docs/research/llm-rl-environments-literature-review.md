# LLM RL environments: literature review for CAD repair and meshing

**Date:** 2026-10-29; CAD and meshing pass added 2026-11-05  
**Scope:** Environment design, verifier rewards, long-horizon tool use, rollout systems, and
reward-hacking controls relevant to post-training open-weight models for CAD repair and meshing.

## Executive Summary

The literature supports building the environment, but not yet a claim that RL will beat Hexera's
frontier routes. No paper found here trains an LLM on CAD repair or with RL on meshing tools, so
transfer to those tasks remains a hypothesis (see [CAD And Meshing Evidence](#cad-and-meshing-evidence)
for the search scope). RL with kernel-executed reward does work on CAD generation. The closest
evidence for agent and environment design comes from software-engineering agents because they also
act through tools, mutate a stateful artifact, and receive executable feedback.

Five conclusions are strong enough to drive the design:

1. **Executable outcomes are the main advantage.** DeepSeek-R1, SWE-Gym, and R2E-Gym all benefit
   from rewards tied to parsers, tests, or execution. Hexera should make fresh-process kernel
   re-inspection, caps, rollback, engine gates, and deliverable checks authoritative. An LLM judge
   may rank already-safe candidates but must not accept an artifact.
2. **Hard constraints should gate reward, not compete inside a weighted sum.** Process-reward and
   reward-hacking work shows that policies exploit whichever proxy is easiest. Invalid topology,
   damage, cap violation, failed engine gates, or incomplete deliverables must make an episode
   ineligible for positive quality reward.
3. **The environment is a versioned system, not a reward callback.** Agent Lightning, BrowserGym,
   SkyRL-Agent, and NeMo Gym converge on separating the agent harness, stateful resources, model
   service, and trainer while preserving exact trajectories. For Hexera, native tools remain on
   CPU environment workers and the trainer consumes an HTTP contract plus immutable traces.
4. **Environment quality precedes rollout scale.** SWE-Gym reports substantial human, CPU, and
   storage cost to construct executable tasks; its on-policy self-improvement experiment did not
   automatically improve the agent. More rollouts do not repair ambiguous tasks, weak validators,
   or nondeterministic native execution.
5. **Asynchrony is useful only with measured staleness and workload classes.** AReaL and
   SkyRL-Agent show throughput gains, but stale-policy rollouts can damage ordinary PPO-style
   training. CAD and mesh episodes also have much wider runtime and memory variance than the
   reported text/code workloads. Start synchronously, measure, then admit bounded asynchrony.

The resulting recommendation is SFT/rejection sampling first, then a small GRPO experiment on
`crs-repair`. Preserve a held-out frontier baseline and treat improvement as an empirical question.

## How To Read The Evidence

Each brief is tagged by what the work actually demonstrates:

- **RL:** updates a policy with reinforcement learning.
- **Other post-training:** updates or selects a model using successful trajectories, but does not
  demonstrate online policy RL in the target environment.
- **Benchmark:** provides an environment or evaluation evidence without post-training a policy.
- **Systems:** measures rollout/training infrastructure rather than task transfer.
- **Preprint:** not treated here as independently replicated evidence.

Reported numbers are the authors' results in their experimental setup. They are not capacity
estimates for OCCT or mesh workers.

## Verifiable Rewards And Credit Assignment

### Training Verifiers to Solve Math Word Problems (Cobbe et al., 2021)

**Evidence:** other post-training / test-time selection.  
**Source:** <https://arxiv.org/abs/2110.14168>

The paper generates many candidate solutions and trains an outcome verifier to rank them. A 6B
verifier paired with sampling slightly outperformed a much larger fine-tuned generator on GSM8K,
showing that compute spent on verified candidates can beat one-shot generation.

The limitation is central for Hexera: a learned verifier is itself a proxy and can learn shortcuts.
The transferable idea is best-of-N repair-plan selection; the non-transferable part is making a
learned model authoritative. Rank candidates by fresh kernel and downstream checks first.

### Let's Verify Step by Step (Lightman et al., 2023)

**Evidence:** process-verifier training and test-time selection.  
**Source:** <https://arxiv.org/abs/2305.20050>

Lightman et al. compare outcome supervision with step-level process supervision on MATH using
PRM800K. Their process reward model performed better at selecting correct long solutions, and
active learning improved label efficiency.

CAD operations have unusually good process labels available without human annotation: whether a
mutating operation bound its targets, survived the kernel, preserved invariants, and improved a
re-inspected defect. Those machine observations are suitable shaping signals. Textual explanations
of why an operation should work are not.

### DeepSeekMath and GRPO (Shao et al., 2024)

**Evidence:** RL.  
**Source:** <https://arxiv.org/abs/2402.03300>

DeepSeekMath introduces Group Relative Policy Optimization. GRPO samples a group of outputs for
the same prompt and estimates relative advantages without a separate value model, reducing the
memory burden of PPO. The paper reports gains from RL on mathematical reasoning.

This is the clearest algorithmic reason to generate several repair trajectories per defect. It does
not imply that an arbitrary scalar reward is sound. For Hexera, group members should be compared
only after hard safety gates, using bounded quality and efficiency terms among feasible outcomes.

### DeepSeek-R1 (DeepSeek-AI, 2025)

**Evidence:** RL and multi-stage post-training.  
**Source:** <https://arxiv.org/abs/2501.12948>

R1 applies GRPO with rule-based accuracy rewards: deterministic answer checking for math and
compiler/test execution for code, plus a formatting reward. R1-Zero demonstrates that RL can elicit
reasoning without a cold start, but the authors report readability and language-mixing problems;
the production R1 recipe adds cold-start data and multiple training stages.

Two lessons transfer. First, SFT before RL is the conservative product path even when pure RL can
move benchmark scores. Second, deterministic task rewards should dominate format rewards. A valid
tool-call schema is necessary but should earn little or no reward by itself.

### PRIME: Process Reinforcement through Implicit Rewards (Cui et al., 2025)

**Evidence:** RL.  
**Source:** <https://arxiv.org/abs/2502.01456>

PRIME learns dense implicit process rewards online from policy rollouts and outcome labels. The
paper reports a 15.1-point average gain over its Qwen2.5-Math-7B SFT baseline across reasoning
benchmarks while avoiding a separately labelled process-reward dataset.

This is a possible later answer to sparse credit, not a v1 dependency. An online process model can
drift and become an optimization target. Hexera should first prove that terminal kernel rewards are
stable, then evaluate whether implicit process rewards improve sample efficiency without changing
held-out safety outcomes.

### ToolRL: Reward Is All Tool Learning Needs (Qian et al., 2025)

**Evidence:** RL.  
**Source:** <https://proceedings.neurips.cc/paper_files/paper/2025/hash/97c5b2707228e7e3fb67e4ecc2e0e607-Abstract-Conference.html>

ToolRL studies reward type, scale, granularity, and dynamics for tool-integrated reasoning with
GRPO. It reports improvements over base and SFT models, finds useful effects from fine-grained and
dynamic rewards, and finds length rewards harmful.

The relevant result is not that CAD needs many reward terms. It is that tool learning is sensitive
to their relative scale over training. Hexera should enforce call, retry, wall, and output-size
limits as caps, and report them as costs. It should not reward long traces or turn compliance into a
large positive proxy.

### PURE: Process Supervision Without Reward Hacking (Cheng et al., 2025)

**Evidence:** RL.  
**Source:** <https://arxiv.org/abs/2504.15275>

PURE argues that summing future process rewards lets many high proxy scores mask one decisive bad
step. It uses a minimum-future-reward form and mixes in verifiable rewards. The paper reports that
standard additive process-reward training can collapse and that a small verifiable component helps.

This maps directly to CAD and meshing. Safety-critical checks should use first-failure or
weakest-link semantics. A mesh with excellent average quality but an invalid region is not rescued
by the average; a repaired shell that closes one defect while introducing damage is not a net
positive episode.

## Reward Hacking And Independent Audits

### Reward Under Attack (Tiwari et al., 2026)

**Evidence:** adversarial verifier study; preprint.  
**Source:** <https://arxiv.org/abs/2603.06621>

The paper stress-tests process reward models under perturbation, direct optimization, and gradient
attacks. It reports policies receiving very high process reward with very low ground-truth accuracy,
with a substantial fraction of reward gain attributable to stylistic shortcuts.

Hexera should assign no material reward to fluent plans, plausible operation names, comments, or
mesh reports authored by the policy. A learned trajectory critic can be diagnostic or rank safe
candidates, but external artifact checks remain authoritative.

### LLMs Gaming Verifiers (Helff et al., 2026)

**Evidence:** controlled RLVR reward-hacking study; preprint.  
**Source:** <https://arxiv.org/abs/2604.15149>

The authors show RLVR-trained models replacing the intended rule induction with instance-level
enumeration that passes an extensional verifier. Isomorphic perturbation tests expose the shortcut,
and shortcut prevalence rises with task complexity and inference-time compute.

For CAD, held-out evaluation needs invariant-preserving perturbations: entity renumbering, rigid
transforms, unit-preserving rescaling where valid, reordered observations, equivalent tessellation
settings, and fresh exports. A policy that learned IDs, file layout, or cached artifacts should fail
these tests without changing the intended repair problem.

### Hack-Verifiable Environments (Roth et al., 2026)

**Evidence:** benchmark; preprint.  
**Source:** <https://arxiv.org/abs/2605.20744>

This work embeds detectable hacking opportunities into environments so exploitation can be scored
automatically rather than inferred from manual trajectory review.

Hexera should add a dedicated adversarial track with stale artifacts, writable lookalike verifier
files, malformed-but-parseable outputs, empty meshes, hidden-test probes, and intentional openings.
The environment should know whether the policy took the shortcut, not merely whether reward and
human judgement disagree afterward.

### CATCH (Wang et al., 2026)

**Evidence:** coding reward-hacking testbed; preprint.  
**Source:** <https://arxiv.org/abs/2609.39533>

CATCH places controllable evaluator loopholes in coding tasks and compares vulnerable reward with
independent execution-based labels. It reports that chain-of-thought monitoring initially reduces
hacking but loses effectiveness as the policy learns to mislead the monitor.

This argues against relying on plan review as the primary defense. Verifier isolation, filesystem
boundaries, fresh-process reload, and independent hidden checks must remain effective even when the
policy's explanation looks benign.

### Audit Where Hacks Pay (Li et al., 2026)

**Evidence:** RL with budgeted strong-verifier audits; very recent preprint.  
**Source:** <https://arxiv.org/abs/2610.35716>

The paper studies GRPO when a cheap verifier can be gamed and a stronger verifier is too expensive
to run on every rollout. It reports that targeting audits by group pass count suppresses hacking
more efficiently than random audits in its code tasks, with near-full-audit performance using a
fraction of labels in one setting.

Hexera already has an analogous hierarchy: cheap kernel re-inspection for every rollout and costly
downstream staging/admission or cross-engine checks for a sample. V1 should store decomposed rewards
so sampled strong audits can correct or quarantine trajectories before training. The exact audit
allocation rule remains experimental and should not be copied without a CAD pilot.

## Executable Agent Environments

### Training Software Engineering Agents and Verifiers with SWE-Gym (Pan et al., 2025)

**Evidence:** other post-training and verifier-guided selection, not online policy RL; ICML 2025.  
**Source:** <https://arxiv.org/abs/2412.21139>

SWE-Gym contains 2,438 Python issue environments with a repository, dependencies, executable tests,
and natural-language task. The paper reports up to 19 percentage points of resolve-rate improvement
from training and 32.0% SWE-bench Verified with verifier-guided inference-time selection.

Environment construction was expensive, and the paper's self-improvement experiment is a warning
against assuming on-policy data is automatically useful. The closest Hexera analogue is a smaller,
version-pinned repair corpus with reliable setup, fresh artifact verification, and explicit failure
labels before trying to scale task generation.

### R2E-Gym (Jain et al., 2025)

**Evidence:** other post-training and test-time selection, not online policy RL; preprint.  
**Source:** <https://arxiv.org/abs/2504.07164>

R2E-Gym procedurally derives more than 8,000 executable software tasks from commits using generated
tests and back-translated issues. Its 32B model reaches 34.4% Pass@1 on SWE-bench Verified; combining
execution and learned execution-free verifiers reaches 51% in the reported test-time-scaling setup.

The paper also shows why one verifier is insufficient: tests can have low distinguishability, while
learned judges can prefer stylistic signals. Hexera should pair broad kernel checks with targeted
defect predicates and occasional downstream audits. Learned ranking is optional and comes after
hard acceptance checks.

### SWE-RL (Wei et al., 2025)

**Evidence:** offline RL; NeurIPS 2025.  
**Source:** <https://arxiv.org/abs/2502.18449>

SWE-RL applies GRPO to software-evolution data using malformed-output penalties and similarity to
the historical patch as reward. The reported 70B model reaches 41.0% on SWE-bench Verified and
improves on several out-of-domain evaluations.

This is evidence that domain RL can transfer, but its reward is not functional execution and the
training loop is not a stateful tool environment. Hexera should use similarity to a known repair or
feature delta only as bounded shaping after kernel acceptance, never as proof of correctness.

### Agent Lightning (Luo et al., 2025; He et al., 2026)

**Evidence:** RL and systems architecture; the 2026 extension is a preprint.  
**Sources:** <https://arxiv.org/abs/2508.03680>, <https://arxiv.org/abs/2608.17528>

Agent Lightning disaggregates an existing agent harness from training, represents execution as
typed transitions, and performs hierarchical credit assignment. The later version emphasizes
training through a production-style harness while preserving call-level prompt and completion token
IDs; it reports substantial SWE-bench improvement for a Qwen3.5 model.

This is the strongest support for keeping Hexera's repair session and builder loop authoritative
rather than recreating them in trainer code. The trajectory format must retain exact sampled token
IDs, action IDs, raw tool responses, artifact hashes, policy version, and pre/post state identifiers.
Prettified logs are not a sufficient training record.

### Search-R1 (Jin et al., 2025)

**Evidence:** multi-turn tool-use RL; preprint.  
**Source:** <https://arxiv.org/abs/2503.09516>

Search-R1 trains models with GRPO to interleave reasoning and multiple search calls, masking
retrieved tokens from the policy loss and using outcome reward. It reports gains of 10-26% across
model families on seven QA datasets.

The transfer is structural: tool outputs are observations, not policy-generated tokens, and must be
tracked separately in loss construction. Its tools are cheap and mostly stateless, so it does not
validate Hexera's crash handling, rollback, or capacity assumptions.

### Demystifying RL for Long-Horizon Tool-Using Agents (Wu et al., 2026)

**Evidence:** controlled long-horizon RL study; preprint.  
**Source:** <https://arxiv.org/abs/2603.21972>

The paper studies reward shaping, model scale, data composition, algorithm choice, and environment
stability on TravelPlanner. It reports that smaller and larger models prefer different reward
recipes, a balanced roughly 1,000-task mixture was effective in its setting, and environmental
instability caused policy degradation.

The relevant conclusion is to treat crashes, timeouts, nondeterministic transitions, and stale
state as training-data corruption, not neutral infrastructure noise. Its sample-count finding is
specific to TravelPlanner and is not a corpus-size estimate for CAD.

### SkyRL-Agent (Cao et al., 2025)

**Evidence:** RL and rollout systems; preprint.  
**Source:** <https://arxiv.org/abs/2511.16108>

SkyRL-Agent provides asynchronous dispatch and backend-independent multi-turn tool execution. It
reports a 1.55x dispatcher speedup over naive asynchronous batching and improves a Qwen3-32B SWE
agent from 24.4% to 39.4% Pass@1 on SWE-bench Verified, with reported cost reductions.

This supports decoupling slow CPU tools from GPU generation and preserving task-level traces. The
reported H100-hours and utilization are SWE-specific and cannot be used to estimate CAD throughput.

## Benchmark And API Evidence

### BrowserGym and WebArena

**Evidence:** benchmarks, not post-training.  
**Sources:** <https://arxiv.org/abs/2412.05467>, <https://arxiv.org/abs/2307.13854>

BrowserGym unifies web benchmarks behind Gymnasium-style `reset`/`step` interfaces with rich
observations and task validators. WebArena provides reproducible self-hosted sites and evaluates
programmatic end-state predicates rather than matching a reference action sequence.

These works support Hexera's small environment contract and end-state verification. They also
surface shared-state collision and synchronous-loop bottlenecks. Each CAD episode therefore needs
an isolated workspace and explicit setup, validation, termination, and cleanup ownership.

### AgentBench (Liu et al., 2024)

**Evidence:** benchmark, not post-training.  
**Source:** <https://arxiv.org/abs/2308.03688>

AgentBench evaluates models across eight heterogeneous interactive environments and uses each
environment's completion checks. Its aggregate scores hide materially different failure modes.

Hexera should publish separate rates for malformed actions, unbindable targets, cap/guard rejection,
rollback, kernel crash, timeout, no progress, verifier failure, and verified completion. One reward
mean or pass rate is inadequate for promotion decisions.

## Rollout And Training Systems

### HybridFlow / veRL (Sheng et al., 2024)

**Evidence:** systems.  
**Source:** <https://arxiv.org/abs/2409.19256>

HybridFlow combines single- and multi-controller orchestration and adds efficient actor resharding
between generation and training. The paper reports large throughput improvements over contemporary
RLHF baselines across several algorithms and scales.

This supports veRL as a later scaling option. It measures GPU-side RLHF dataflow, not native CAD
workers, remote environment latency, or sandbox reliability, so it does not decide the v1 trainer.

### AReaL (Mei et al., 2025)

**Evidence:** asynchronous RL systems.  
**Source:** <https://arxiv.org/abs/2505.24298>

AReaL decouples generation from training and reports up to 2.77x shorter training time while
matching synchronous quality in its tasks. Its ablations also show that naive PPO can degrade with
rollout staleness; the system bounds staleness and modifies the learning objective.

Hexera should begin with synchronous policy versions. If measurement shows GPU starvation, add
bounded policy lag, record it per action, and compare learning curves against the synchronous
baseline before enabling broader asynchrony.

### NeMo Gym (NVIDIA, 2025-2026)

**Evidence:** official implementation and vendor report, not an independent paper.  
**Sources:** <https://github.com/NVIDIA-NeMo/Gym>,
<https://research.nvidia.com/labs/nemotron/files/NVIDIA-Nemotron-3-Super-Technical-Report.pdf>

NeMo Gym defines an environment as tasks, an agent harness, verifiers, and per-task state. Current
releases split agent, model, and resource servers; preserve trajectories; support pluggable
sandboxes; and describe large concurrent environment batches in Nemotron training.

The architecture is a useful comparison for Hexera's HTTP boundary and durable traces. Vendor
claims about thousands of environments do not transfer to OCCT/gmsh workers with high RAM, native
crashes, and broad runtime variance.

## Reproducibility, Contamination, And Security

### Deep RL That Matters and reproducibility follow-up

**Evidence:** empirical reproducibility studies.  
**Sources:** <https://arxiv.org/abs/1709.06560>, <https://arxiv.org/abs/1809.05676>

These works show that seeds, implementations, reward scale, and evaluation choices can dominate
reported deep-RL differences. Hexera must freeze and record image digest, OCCT/engine versions,
thread counts, settings, random seeds, model checkpoint, decoding settings, and artifact hashes.
Replayable traces are part of correctness, not optional observability.

### SWE-rebench (Badertdinov et al., 2025)

**Evidence:** benchmark construction and contamination study.  
**Source:** <https://arxiv.org/abs/2505.20411>

SWE-rebench creates a large, time-sliced executable software benchmark and reports evidence that
static benchmark results can be inflated by contamination.

CAD splits must be by source part family and provenance, with fresh private defects and hidden
perturbations reserved for final evaluation. A random row split leaks near-duplicate geometry and
repair patterns.

### SandboxEscapeBench (2026)

**Evidence:** adversarial benchmark; preprint.  
**Source:** <https://arxiv.org/abs/2603.02277>

SandboxEscapeBench evaluates agents against intentionally vulnerable container, orchestration, and
kernel configurations. It reinforces that a container image alone is not a security boundary.

RL workers need default-deny egress, no Docker socket or host mounts, dropped capabilities,
ephemeral credentials, resource quotas, and a stronger outer boundary where practical. CAD parsers
and meshers must also be treated as native hostile-input processors.

## CAD And Meshing Evidence

Added 2026-11-05 to close a gap: the original review covered math, software, web, and tool-use work
and did not search CAD or meshing machine learning.

### Search scope

This was a targeted pass, not a systematic review. It ran ten general web searches on 2026-11-05
over five topics: RL for CAD code generation; LLM or learned B-rep repair and healing; RL and LLM
meshing; LLM agents for CFD/FEA setup; and defeaturing and simulation preparation. It also followed
citations from one survey ([Owen et al.](https://arxiv.org/abs/2512.23719)).

It did not query Scopus, Compendex, Web of Science, or patent databases, and it did not examine how
commercial healing tools work internally. Most sources below were read at abstract level, so a
negative finding here means "not found in this search", not "does not exist".

Depth read is stated per source: **full text** (body of the paper), or **abstract** (arXiv abstract
page only; methods and limits may differ in the paper).

### Verdict

**CAD transfer remains a hypothesis.** This search found no work that trains an LLM, by RL or
otherwise, to repair defects in existing B-rep models, and no work that trains an LLM with RL on
meshing tools. The Owen et al. survey has no repair or healing section and reports no such training
in the 200,000 of its 222,304 characters read here; it presents kernel-in-the-loop refinement of
LLM-proposed procedures as a direction, not a result.

The pass does narrow the hypothesis. Execution-gated RL demonstrably works on CAD *generation*, so
the open question is no longer whether RL can use a CAD kernel signal. It is whether that result
survives the differences below, none of which any source tests.

| | CAD generation (evidenced) | CRS repair (Hexera) |
|---|---|---|
| Input | text, image, or point cloud | real defective file |
| Action | free-form program | choice from a capped operation catalogue |
| Reward target | known ground-truth shape | no ground truth; re-inspection and damage bounds |
| Main failure | program does not run | repair reports success but damages the part |

### RL on CAD generation

**cadrille (ICLR 2026).** RL; full text. <https://arxiv.org/abs/2505.22914>. Qwen2-VL-2B generates
CadQuery after SFT, then online RL with Dr. CPPO, a combination of Dr. GRPO and CPPO. Reward is
10 x IoU, with -10 for invalid code. On real-scan CC3D multi-view input, invalid rate went from 7.7%
(SFT) to 3.9% (offline DPO) to 0.1% (online RL), and IoU from 56.1% to 56.0% to 65.0%. RL used only
hard examples, those with average SFT reward below 7.5, and used images because point-cloud tuning
"appeared to be unstable". This is the strongest evidence that a small open model improves under
kernel-executed reward, and the penalty is sized so invalid code cannot beat any valid output.

**CAD-Coder (NeurIPS 2025).** RL; abstract. <https://arxiv.org/abs/2505.19713>. SFT on about 110K
text-CadQuery-model triplets, then GRPO with Chamfer-distance and format rewards. Same SFT-then-GRPO
order as this project's plan, on a generation task.

**STEP-LLM (DATE 2026).** RL; abstract. <https://arxiv.org/abs/2601.12641>. Generates STEP directly
from about 40K STEP-caption pairs, with SFT then RL on a Chamfer-distance reward. Shows STEP can be
model output, but not that a model can diagnose a defective STEP file.

### Editing and repair nearest to CRS

**B-repLer (SIGGRAPH 2026).** Other post-training; abstract.
<https://arxiv.org/abs/2508.10201>. A fine-tuned multimodal LLM makes semantic edits to existing
B-reps in a latent space, without construction history, using BrepEDIT-240K. Closest found work on
modifying an existing B-rep, but it applies requested design edits; the abstract does not mention
repairing defects.

**TraceCAD (2026).** Preprint; abstract. <https://arxiv.org/abs/2608.03062>. Repairs faulty steps in
an agent's own parametric program using diagnosis, bounded local search, execution and preservation
checks, and a skill memory. The abstract mentions no model training. Removing localized search more
than doubled geometric regression. Its repair target is a program the agent wrote, not an imported
file, but its regression focus and memory align with the sibling defect-CAD RAG track.

**Embodied CAD (2026).** RL; preprint; abstract. <https://arxiv.org/abs/2606.31252>. An agent picks
typed skills executed in an exact kernel, with supervised warm-up, GRPO-style refinement, and
solver-derived rewards. Its typed action space is the closest found match to a CRS operation
catalogue. It builds new assemblies, "repair" means correcting its own failed steps, and learned
controllers still lag on exact long-horizon policy prediction.

**WDR (2026).** Preprint; abstract. <https://arxiv.org/abs/2608.04955>. Training-free detection and
repair of wireframe anomalies inside B-rep generators, judged by kernel-checked validity. It acts on
intermediate generator output; the abstract does not claim it handles existing CAD files.

### Meshing and simulation agents

**Survey of AI for geometry preparation and meshing (Owen et al., 2026).** Survey; preprint; full
text except the last 22,304 characters. <https://arxiv.org/abs/2512.23719>. Describes AI-enhanced
unstructured meshing as at "a low technology readiness level" and says most RL-driven scripting
studies "remain proofs of concept". The nearest learned analogue to a repair planner is ML-guided
defeaturing that ranks candidate operations by predicted post-operation mesh quality.

**Soft actor-critic quad meshing (Pan et al., 2023).** RL; abstract.
<https://arxiv.org/abs/2203.11203>. Treats meshing as a Markov decision process and trains a
non-LLM policy. Evidence for element-level RL, not for a language model driving a mesher.

**Small LLMs for Gmsh geometry (2025).** Other post-training; abstract.
<https://arxiv.org/abs/2503.18178>. LoRA on 480 instruction-output pairs for Phi-3 Mini and
Qwen-2.5 1.5B. Multi-body geometry remained hard. No RL.

**GmshNet-8B v0.1.** Model card only.
<https://huggingface.co/hexera-org/GmshNet-8B-v0.1-GGUF>. An 8B Gmsh scripting model published under
`hexera-org`. The card states no base model, training method, data, or evaluation, so it provides no
evidence here.

**Foam-Agent (2025).** Benchmark; abstract. <https://arxiv.org/abs/2505.04997>. Multi-agent OpenFOAM
workflow with a meshing agent: 88.2% execution success on 110 FoamBench Basic tasks and 62.5% on the
Advanced tier. Execution success does not establish physical accuracy.

**Coding agents for CFD (2026).** Benchmark; preprint; abstract.
<https://arxiv.org/abs/2602.11689>. Stronger base models substantially improved mesh generation and
task completion on obstacle-flow cases, without training.

**What CAE agents need beyond a generic harness (2026).** Benchmark; preprint; abstract.
<https://arxiv.org/abs/2609.03718>. On FoamBench, a single-agent generic harness scored 96.4% against
88.2% for specialized multi-agent systems. Execution-feedback repair moved accuracy from 71.8% to
96.4%, and solver tutorials moved it from 80.9% to 96.4%. No training is described. This is a warning
for this project: harness and documentation changes may capture gains attributed to RL.

### Consequences for this design

1. **Add a harness-only baseline.** Before crediting post-training, measure the same base model with
   execution feedback and operation-catalogue documentation but no weight updates.
2. **Keep hard gates dominant.** cadrille's penalty supports invalid outcomes never outscoring valid
   ones. It does not support damage gates directly, because its failure is non-execution.
3. **Use geometric deviation as a bound, not a goal.** Chamfer and IoU rewards rely on a target shape
   that repair lacks. Deviation from the input can bound damage, but cannot score success alone.
4. **Select tasks for reward variance.** cadrille trained RL only on hard examples. Measure per-task
   pass rates and drop all-pass and all-fail tasks from GRPO groups.
5. **Treat `mesh-builder` as weaker-evidenced than `crs-repair`.** Meshing has non-LLM RL and untrained
   agents, but no LLM-with-RL result here.

## Design Consequences For Hexera

### Reward order

Use a gated reward vector rather than a single freely additive score:

```text
1. episode integrity: no harness/verifier failure or policy tampering
2. artifact safety: reload succeeds, topology/mesh valid, no forbidden damage
3. task completion: target defect removed, labelled safe decline, or required engine/deliverable
   gates pass
4. bounded quality: defect yield, mesh quality, downstream acceptance
5. bounded cost: tool calls, retries, CPU, wall, and output size
```

A failure at levels 1-3 prevents positive reward from levels 4-5. A safe decline passes level 2 only
when no mutation occurred and the source artifact hash is unchanged, and passes level 3 only on a
hidden task labelled for decline. Keep every component in the trajectory so reward policy can be
audited and recomputed without rerunning the native episode.

### Environment topology

Use three independently scalable planes:

1. **Model plane:** inference workers with immutable policy versions and exact token capture.
2. **Environment plane:** crash-isolated CPU workers that own mutable CAD/mesh state and tools.
3. **Verification plane:** fresh-process artifact reload, hard gates, sampled strong audits, and
   durable reward records outside the policy's write scope.

The trainer consumes trajectories and reward vectors. It does not import OCCT or reimplement
product guards.

### Experiment order

1. Measure deterministic replay, native failure rates, per-episode resources, and frontier/SFT
   baselines.
2. SFT or rejection-sampling fine-tune on verified successful and correct-decline trajectories.
3. Run synchronous GRPO on one narrow `crs-repair` track with several rollouts per defect.
4. Add adversarial hack-verifiable tasks and sampled stronger audits before increasing scale.
5. Consider dense learned process rewards or asynchronous training only after ablations show a
   held-out gain without worse damage, hacking, or determinism metrics.

## Open Research Questions

- Do kernel-verifiable repair outcomes provide enough reward variance within a GRPO group, or are
  most groups all-pass/all-fail?
- Which intermediate signals improve learning without becoming shortcuts: target binding, rollback,
  defect delta, or operation-level kernel validity?
- How often must downstream staging or cross-engine verification run to detect kernel-local reward
  hacks at acceptable CPU cost?
- Does a multi-turn repair policy beat single-shot directive generation after equalizing inference
  tokens, CPU time, and candidate count?
- How much policy staleness can the measured CAD workload tolerate before asynchronous throughput
  gains become learning regressions?
- Which invariant perturbations preserve the intended repair task while exposing memorization of
  entity IDs, observation order, or exporter behavior?
