# Responsibility: Verify the patch contract regenerates from intake on retry, independent of any prior workspace.
from meshpipeline.agents.builder.tools import _contract_wall_patch  # noqa: E402
from meshpipeline.agents.builder.workspace import _write_workspace_context_files  # noqa: E402

_PATCHES = [{"name": "aircraft", "type": "wall"}, {"name": "farfield", "type": "farfield"}]


def test_contract_roundtrips_to_intake_wall_name(tmp_path):
    # a bare (retry-like) workspace with no contract yet -> None (the bug's trigger)
    assert _contract_wall_patch(tmp_path) is None

    # regenerate context from state, as the retry path now does
    _write_workspace_context_files(
        workspace=tmp_path, source_path="input.step",
        request_txt="external aero", review_brief_txt="",
        intake_patches=_PATCHES, dimensionality="3D",
    )
    assert (tmp_path / "patches_contract.txt").exists()
    # the wall name the snappy build will use is the CONTRACT name, not 'body'
    assert _contract_wall_patch(tmp_path) == "aircraft"


def test_regeneration_is_independent_of_a_prior_workspace(tmp_path):
    # the fix regenerates from state, so it works even if the previous attempt's
    # workspace was purged (the carry-forward would have had nothing to copy).
    fresh_retry_ws = tmp_path / "attempt_3"
    fresh_retry_ws.mkdir()
    _write_workspace_context_files(
        workspace=fresh_retry_ws, source_path="input.step",
        request_txt="", review_brief_txt="",
        intake_patches=[{"name": "wing", "type": "wall"}], dimensionality="",
    )
    assert _contract_wall_patch(fresh_retry_ws) == "wing"


def test_no_patches_yields_no_contract_name(tmp_path):
    # empty contract (no intake patches) -> None; the build falls back to 'body'
    # only when there is genuinely no contract to honor.
    _write_workspace_context_files(
        workspace=tmp_path, source_path="input.step",
        request_txt="x", review_brief_txt="", intake_patches=[], dimensionality="",
    )
    assert _contract_wall_patch(tmp_path) is None
