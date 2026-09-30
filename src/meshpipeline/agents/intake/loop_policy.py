# Responsibility: Decide what the intake loop does after each round.
# Boundaries: policy over observed rounds; it calls no model.
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.agents.intake import turn
from meshpipeline.agents.intake.diagnostics import IntakeRunExtension
from meshpipeline.agents.intake.executor import IntakeExecutionState, category_of
from meshpipeline.agents.loop.accounting import ToolInvocation
from meshpipeline.agents.loop.driver import RoundDecision, ToolOutcome
from meshpipeline.contracts import engineering_text
from meshpipeline.contracts.agent_loop import (
    AgentRole,
    LoopExit,
    LoopLimits,
    LoopStage,
    LoopTally,
    ProgressObservation,
)

logger = logging.getLogger(__name__)

# The application-rendered terminals, in the priority the batch is resolved with. A new engine
# selection outranks a submission (the user is choosing again); a submission summary wins only if
# nothing above it did. An impossible admission is NOT a terminal any more: it goes back to the
# model as a tool result to repair or put to the user (executor.py) - though it still voids the
# submission beneath it there, so nothing refused is ever delivered.
TERMINAL_PRIORITY = ("selection_prompt", "submit_summary")

# Words that carry no content when two questions are compared: grammar, the assent vocabulary
# and the scaffolding every proposal shares ("I would go with ... - ok, or tell me what differs").
_STOP = frozenset(
    "a an and are as at be but by can do for from i if in is it of on or so that the this to we "
    "what which will with would you your ok okay yes no go tell me differs right correct please "
    "should want like also then now".split())
_MIN_WORDS = 4
#: Jaccard similarity of two questions' content words at or above which they are the same ask.
REPEAT_THRESHOLD = 0.6


#: What separates two words. A word keeps its superscript digits and signs and its Greek letters,
#: so 10⁵ is not 10⁶, y⁺ is not y, and k-ω is not k-ε.
_WORD_BREAK = re.compile(r"(?:[^\w+⁺⁻₊₋]|_)+")


def _words(text: str) -> frozenset[str]:
    # Both sides in one spelling: a question stored before replies were made plain still carries
    # its LaTeX, and \(y^+\) must count as the same word as y⁺.
    plain = engineering_text.plain(str(text or ""))
    return frozenset(w for w in _WORD_BREAK.sub(" ", plain.casefold()).split()
                     if w not in _STOP)


def repeats_a_question(candidate: str, prior: tuple[str, ...] | list[str]) -> bool:
    # A reply repeats a question when it asks what an earlier assistant message asked, in whatever
    # wording: the same content words, allowing for a rephrase. Compared whole, because a proposal
    # follows its question ("Fluid? I would go with air - ok?") and the two together are the ask.
    if "?" not in str(candidate or ""):
        return False
    mine = _words(candidate)
    if not mine:
        return False
    for text in prior:
        if "?" not in str(text or ""):
            continue
        theirs = _words(text)
        if not theirs:
            continue
        if mine == theirs:
            return True                     # the same words again, however few
        if (len(mine) >= _MIN_WORDS and len(theirs) >= _MIN_WORDS
                and len(mine & theirs) / len(mine | theirs) >= REPEAT_THRESHOLD):
            return True                     # a rephrase - judged only on enough words to judge
    return False


#: Words that make the engine the subject of the user's message, besides an engine's own name.
_ENGINE_TALK = re.compile(r"\b(?:engines?|meshers?|toolchains?|meshing (?:tool|program|software)s?)\b",
                          re.IGNORECASE)


def user_raised_the_engine(text: str) -> bool:
    """Whether the user's message is about the engine - names one, or asks about the mesher."""
    from meshpipeline.agents.intake import vocabulary as _vocab
    return bool(_vocab.engines_named_in(str(text or ""))) or bool(_ENGINE_TALK.search(str(text or "")))


def engine_nudge(reply: str, *, selection: dict | None, latest_user_msg: str,
                 revision: str | None = None, user_msg_count: int | None = None) -> str | None:
    """The note a finished reply earns for how it speaks of the engine, or None.

    The engine is settled by ONE question - the application's own, with the model's reason -
    and after that the user reads it again only in the summary they approve. A real session
    opened three replies in a row with it instead: "I'll record snappyHexMesh as the meshing-
    engine assumption", "I'll use snappyHexMesh as an unstated assumption", "I'll carry forward
    snappyHexMesh as the unconfirmed engine assumption". So a reply that names an engine while
    the user did not bring engines up is sent back: with the engine settled, to leave it out;
    with nothing settled and ONE engine named (carried as an assumption, or asked in the model's
    own words), to propose it through the one question instead.

    Left alone: a reply listing several engines (a menu, not an engine carried); a "Heads-up:"
    line (a soft limitation of the chosen engine is raised once before the summary, by name); the
    reply to the very message that settled it ("sure" -> "Gmsh it is. Flow speed?") - one
    acknowledgement is an answer, the same line on every later turn is the restating; and the
    reply to an answer of the engine question itself ("why?" to "I'd mesh this with X ... OK?"),
    whose subject is the engine whatever words the user chose."""
    from meshpipeline.agents.intake import engine_selection as es
    from meshpipeline.agents.intake import vocabulary as _vocab

    named = _vocab.engines_named_in(str(reply or ""))
    if not named or user_raised_the_engine(latest_user_msg):
        return None
    state = es.state_of(selection)
    if (state == es.PROPOSED and user_msg_count is not None
            and int((selection or {}).get("proposed_msg_count", -2)) + 1 == int(user_msg_count)):
        return None
    if state == es.CONFIRMED:
        settled_now = revision is not None and (selection or {}).get("confirmed_revision") == revision
        if settled_now or "heads-up" in str(reply).casefold():
            return None
        return turn.ENGINE_SETTLED_NUDGE
    if len(named) == 1:
        return turn.ENGINE_UNSETTLED_NUDGE
    return None


@dataclass
class IntakeLoopPolicy:

    exec_state: IntakeExecutionState
    executor: Any                              # IntakeToolExecutor - the ONLY dispatch site
    limits_: LoopLimits = field(default_factory=LoopLimits)
    role: AgentRole = AgentRole.intake
    #: What the assistant has already said in this conversation - the questions already asked.
    prior_questions: tuple[str, ...] = ()
    #: Whether a reply is held to "the engine is asked once, then left alone" (`engine_nudge`).
    #: Off for the turn after a run has ended, where the last run's engine is the subject.
    engine_guard: bool = True

    malformed_calls: int = 0
    repeated_questions: int = 0                # replies sent back for asking the same thing again
    engine_nudges: int = 0                     # replies sent back for how they spoke of the engine
    plaintext_text: str = ""                   # the model's reply, returned - never diagnosed
    finish_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    _last_signature: str = ""
    _round_advanced: bool = False

    # LoopDriver: the runner
    def limits(self) -> LoopLimits:
        return self.limits_

    def category_of(self, tool: str) -> str:
        return category_of(tool)

    def forced_tool(self, tally: LoopTally) -> str | None:
        return None

    def before_round(self, tally: LoopTally, messages: list[dict]) -> None:
        self._round_advanced = False

    def is_supersession(self, exc: BaseException) -> bool:
        return False

    async def execute(self, invocation: ToolInvocation) -> ToolOutcome:
        result = await self.executor.run(invocation.tool, invocation.parsed)
        if result.malformed:
            self.malformed_calls += 1
            return ToolOutcome(content=result.content, accepted=False)
        if result.advanced:
            self._round_advanced = True
        return ToolOutcome(content=result.content, accepted=result.accepted)

    # progress
    def observe(self, tally: LoopTally) -> ProgressObservation:
        signature = self.exec_state.authorization_signature()
        advanced = self._round_advanced
        self._round_advanced = False
        if advanced:
            self._last_signature = signature
            return ProgressObservation(made_progress=True, signature=signature,
                                       detail="the requirements advanced")
        return ProgressObservation(made_progress=False, signature=signature,
                                   detail="no material change to the requirements")

    def correction(self, stage: LoopStage, tally: LoopTally,
                   observation: ProgressObservation) -> str | None:
        return None

    def on_plaintext(self, tally: LoopTally) -> RoundDecision:
        # THE SAME QUESTION IS NEVER ASKED TWICE - enforced here, not left to the prompt. A reply
        # that asks what the user was already asked is sent back ONCE, inside the turn, with the
        # rule: their reply was their answer, take your own proposal and move on. Once, so a model
        # that insists still ends the turn and the user is never left waiting on a loop; that
        # second reply is delivered as written. And only while another round may start: a nudge
        # the budget cannot honour would end the turn with no reply at all.
        left = tally.remaining_rounds(self.limits_)
        may_retry = left is None or left > 0
        # The engine first: a reply carrying an unsettled engine is fixed by proposing it, which a
        # repeat nudge ("take your own proposal") would get wrong. Once, like the repeat below.
        note = self._engine_note() if may_retry and self.engine_nudges == 0 else None
        if note:
            self.engine_nudges += 1
            logger.info("Intake: the reply names the engine the user did not bring up - sent "
                        "back once (%s)", "settled" if note == turn.ENGINE_SETTLED_NUDGE else "unsettled")
            return RoundDecision(message=note, complete=False)
        if (self.repeated_questions == 0 and may_retry
                and repeats_a_question(self.plaintext_text, self.prior_questions)):
            self.repeated_questions += 1
            logger.info("Intake: the reply asks a question the user was already asked - sent "
                        "back once to take the proposal and move on")
            return RoundDecision(message=turn.REPEAT_NUDGE, complete=False)
        return RoundDecision(message="", complete=True, payload=self.plaintext_text,
                             exit=LoopExit.turn_complete)

    def _engine_note(self) -> str | None:
        st = self.exec_state
        # Not when the engine is the business of the turn: a comparison the user asked for, or a
        # refusal of the selected engine (its reply is checked by refusal.settle).
        if (not self.engine_guard or st.recommended_this_turn or st.rec_authorized
                or st.admission_refusal is not None):
            return None
        return engine_nudge(self.plaintext_text, selection=st.selection,
                            latest_user_msg=st.latest_user_msg, revision=st.revision,
                            user_msg_count=st.user_msg_count)

    async def close_out(self, tally: LoopTally) -> ToolOutcome | None:
        terminal = self.resolve_terminal()
        if terminal is None:
            return None
        return ToolOutcome(content=terminal, accepted=True, terminal=True, payload=terminal)

    def note_round(self, round_result: Any) -> None:
        # The model's words enter here and nowhere else, so this is where its math markup becomes
        # plain engineering text: \(y^+=30\text{–}300\) reaches the chat, the session and the
        # repeat check above as "y⁺ = 30–300". Backticked patch and file names stay verbatim.
        self.plaintext_text = engineering_text.plain((round_result.assistant_text or "").strip())
        self.finish_reason = round_result.finish_reason or "unknown"
        self.input_tokens = round_result.input_tokens
        self.output_tokens = round_result.output_tokens

    # post-batch resolution
    def resolve_terminal(self) -> str | None:
        st = self.exec_state
        for name in TERMINAL_PRIORITY:
            text = getattr(st, name)
            if text:
                if name != "submit_summary":
                    # Nothing below this priority survives: a submission the batch superseded is
                    # not delivered, and its arguments are not returned to the caller.
                    st.submit_args = None
                    st.submit_summary = None
                return text
        return None

    # diagnostics
    def extension(self) -> IntakeRunExtension:
        st = self.exec_state
        return IntakeRunExtension(
            missing_fields=tuple(st.val_errors_seen),
            tools_invoked=tuple(st.tools_invoked),
            submit_attempts=st.submit_attempts,
            submit_rejections=st.submit_rejections,
            authorization_state=("authorized" if st.submit_args is not None
                                 else ("unauthorized" if st.submit_attempts else "")),
            selection_state=("confirmed" if (st.selection or {}).get("confirmed")
                             else ("proposed" if st.selection else "")),
            approval_state=str((st.approval or {}).get("status", "") or ""),
            recommendation_turn=st.recommended_this_turn,
            canonical_revision=str(st.revision or ""),
            repeated_questions=self.repeated_questions,
            engine_nudges=self.engine_nudges)


__all__ = ["REPEAT_THRESHOLD", "TERMINAL_PRIORITY", "IntakeLoopPolicy", "engine_nudge",
           "repeats_a_question", "user_raised_the_engine"]
