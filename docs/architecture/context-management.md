# Context management

One question this document answers: **what happens when an agent's conversation grows towards the
model's context window?** Rounds and deadlines bound how *long* an agent runs
([development/gates.md](../development/gates.md) covers the gates); this document covers how much it
can be holding while it does.

Each agent answers it differently, because each has a different shape of work. Nothing here is
shared machinery: there is no common compaction layer, and adding one would have to earn its place
against three genuinely different problems.

## The three agents at a glance

| agent | rounds | the pressure | mechanism |
|---|---|---|---|
| **Builder** | 60 (45 on retry) | authors and re-authors a mesh spec, running tools whose output it must keep | knowledge-block checkpoint |
| **Reviewer** | 30 | inspects a finished mesh from assembled evidence | none - evidence is assembled once |
| **Intake** | 20 | a conversation with a person | per-turn budget nudge |

## Builder: the knowledge-block checkpoint

The builder is the only agent that can genuinely run out of room. It authors a spec, runs a mesh,
reads the failure, edits, and runs again - and every tool result it needs stays in the conversation.

`agents/builder/context.py` owns the measurement and the thresholds;
`agents/builder/context_prep.py` owns the decision and the two actions.

### Measuring

Tokens are **counted, not estimated**: `tiktoken` with `cl100k_base`, plus a per-message overhead of
4 tokens. Each round takes the larger of the local count and what the provider reported, so an
undercount on either side cannot hide the true size.

### The two thresholds

| | of a 262,144-token window | what happens |
|---|---|---|
| **inject** | 35% (91,750) | the builder is told to write `knowledge_block.txt` **now**, before any other tool call |
| **recover** | 40% (104,857) | the conversation is **replaced**: system prompt + knowledge block + the authored spec, read from disk |

The gap between them is deliberate. Injection asks the builder to summarise while it still has room
to do it well; recovery is the floor that acts whether or not it complied.

A re-injection gap of **10,000 tokens** stops the ask repeating every round - otherwise the window
would be spent on the instruction to save the window.

### What recovery rebuilds from

Recovery never trusts the conversation it is discarding. It rebuilds from **disk**:

- `knowledge_block.txt`, if the builder wrote one - its own summary, in its own words
- the authored mesh spec, read verbatim from the files the ENGINE declares
  (`get_spec_run_files`) - no artifact filename is hardcoded, because a hardcoded per-engine branch
  once left snappy recovery with no spec at all

**If no knowledge block was ever written, recovery still happens.** The builder can ignore the
instruction; the attempt must survive that. The rebuilt message says the block is absent and points
at the spec instead.

### Tool output

`_compress_tool_output` compresses a tool result before it enters the conversation, with a `protect`
list for fields that must survive intact. Separately, a single tool result over 20,000 characters is
truncated at the executor with a `_truncated` marker, so one runaway output cannot swallow the
window on its own.

### What data collection sees

The run record carries both:

| field | meaning |
|---|---|
| `checkpoint_recoveries` | how many times the conversation was rebuilt |
| `checkpoint_recovery_tokens` | the prompt-token count each rebuild fired at |

The count alone says an attempt hit the ceiling; the figures say **where**, which is what the
thresholds can be tuned against later.

The knowledge block itself is captured too, without any special handling: the corpus snapshots every
non-binary file under each `attempt_N/` workspace, so `knowledge_block.txt` is exported as
`workspace/attempt_N/knowledge_block.txt` and can be read beside the run that produced it.

## Reviewer: assembled once, not accumulated

The reviewer receives evidence rather than gathering it over many rounds.
`agents/reviewer/context.py` composes the prompt from the rubric, the workflow and the evidence, and
builder-authored configuration appears as delimiter-neutralised **evidence, never as instruction**.

With 30 rounds and no authoring loop, it has no mechanism and needs none. That is an observation
about its shape, not a measurement: if a review ever begins carrying many rounds of inspection
imagery, this is the assumption to re-test first.

## Intake: a budget nudge, not compaction

Intake talks to a person, so its conversation is short by nature and there is nothing to compress.
`agents/intake/turn.py` assesses a per-turn budget and can nudge the message list mid-turn
(`apply_budget_nudge`), which is turn management rather than context management.

## Changing any of this

The thresholds are percentages of one window constant. If a model with a different window is
adopted, `_BUILDER_CONTEXT_WINDOW` is the single value to change and the thresholds follow.

Two properties are load-bearing and should not be traded away for simplicity:

1. **Recovery reads from disk, not from the conversation.** A summary of a conversation that is
   about to be discarded has no independent source of truth.
2. **Recovery does not depend on the builder having complied.** The floor must hold when the
   instruction was ignored.
