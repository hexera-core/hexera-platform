"""The operator's sentence must say which of four things happened, not one word for all.

The owner's objection: reporting a failure-shaped sentence for a part that needed no work
reads as though the system fell over. A part nobody needed to touch is not a part we failed
on - but a part we genuinely could not repair must still read that way.
"""
from dataclasses import dataclass

from meshpipeline.cad.repair import conservative
from meshpipeline.cad.repair.loop import NO_CANDIDATE, RESOLVED, STALLED


@dataclass
class _V:
    kept: bool = False


@dataclass
class _Run:
    stopped_because: str
    verdicts: tuple = ()
    defects: tuple = ()
    resolved: bool = False
    complete_scan: bool = True


@dataclass
class _Final:
    valid: bool = True


def _say(run):
    return conservative._summary(run, _Final())


def test_a_part_that_needed_nothing_does_not_read_as_a_failure():
    said = _say(_Run(stopped_because=RESOLVED))
    assert "Nothing needed repairing" in said
    assert "could be repaired safely" not in said, "a clean part must not read as a failure"


def test_a_catalogue_with_no_answer_says_so_plainly():
    said = _say(_Run(stopped_because=NO_CANDIDATE, defects=("d",)))
    assert "No repair in this catalogue claims" in said
    assert "a person should look at it" in said, "an unanswered part still needs a human"


def test_a_part_we_tried_and_failed_still_reads_as_a_failure():
    """The guard against making this change flatter the system."""
    said = _say(_Run(stopped_because=STALLED, verdicts=(_V(), _V()), defects=("d",)))
    assert "none held" in said
    assert "a person should look at this" in said
    assert "Nothing needed repairing" not in said


def test_every_no_kept_sentence_states_the_file_is_unchanged():
    """No sentence may leave a reader thinking they received a modified file."""
    for run in (_Run(stopped_because=RESOLVED),
                _Run(stopped_because=NO_CANDIDATE, defects=("d",)),
                _Run(stopped_because=STALLED, verdicts=(_V(),), defects=("d",))):
        assert "unchanged" in _say(run), run.stopped_because
