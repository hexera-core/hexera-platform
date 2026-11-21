"""The one hop that makes the review tier live: a stated threshold must reach the session.

Task 20 built tiered review and Task 27 validated the customer's threshold at the API, but
nothing carried it between them, so no change was ever small enough to auto-accept and the
whole tier was inert. These pin the hop, in both directions.
"""
import inspect

from meshpipeline.cad.repair import conservative, region_loop


def test_repair_step_file_accepts_a_threshold():
    assert "auto_accept_mm" in inspect.signature(conservative.repair_step_file).parameters


def test_run_regions_accepts_the_same_threshold():
    """The names must match: the hop is a verbatim forward, not a translation."""
    assert "auto_accept_mm" in inspect.signature(region_loop.run_regions).parameters


def test_the_threshold_is_forwarded_verbatim_and_not_invented():
    """The call site passes the parameter through - it does not substitute a default."""
    source = inspect.getsource(conservative.repair_step_file)
    assert "auto_accept_mm=auto_accept_mm" in source, (
        "the threshold must be forwarded as given; a literal here would silently ignore "
        "what the customer stated"
    )


def test_an_unstated_threshold_stays_none_not_zero():
    """None and 0.0 are different answers.

    None means the customer stated nothing, so every package goes to the reviewer. A zero
    threshold would mean 'auto-accept only a change that moved nothing', which is a claim the
    customer never made. Defaulting one to the other would be inventing policy.
    """
    assert inspect.signature(conservative.repair_step_file).parameters[
        "auto_accept_mm"].default is None
    assert inspect.signature(region_loop.run_regions).parameters[
        "auto_accept_mm"].default is None
