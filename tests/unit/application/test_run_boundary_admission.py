# Responsibility: Verify a run is admitted only when its patch contract and intent fingerprint still hold.
from __future__ import annotations

import uuid

import pytest

from meshpipeline.application import approved_patch_contract as apc
from meshpipeline.persistence.models import JobStatus

JOB = str(uuid.uuid4())


class _Req:
    def __init__(self, **kw):
        self.approved_patch_contract = None
        self.approved_intent_fingerprint = ""
        self.intake_patches = [{"name": "body", "type": "wall"}]
        self.mesh_engine = "cfmesh"; self.purpose = "external_cfd"
        self.input_kind = "solid-body"; self.dimensionality = "3D"
        self.engine_params = {}
        # the DEFAULT-source fidelity contract: null requested, effective == the default, and
        # the current policy version (enums.assert_fidelity_consistent enforces all three)
        from meshpipeline.pipeline.enums import DEFAULT_MESH_FIDELITY, FIDELITY_POLICY_VERSION
        self.requested_mesh_fidelity = None
        self.effective_mesh_fidelity = DEFAULT_MESH_FIDELITY.value
        self.mesh_fidelity_source = "default"
        self.fidelity_policy_version = FIDELITY_POLICY_VERSION
        self.request_txt = "mesh it"; self.approved_snapshot_id = "snap-1"
        self.geometry_source = None; self.geometry_interpretation = None
        self.__dict__.update(kw)


class _Log:
    def __init__(self): self.errors = []; self.warnings = []
    def error(self, *a, **k): self.errors.append(a)
    def warning(self, *a, **k): self.warnings.append(a)
    def info(self, *a, **k): pass


# the pure gates

def test_a_run_with_no_approved_contract_is_admitted():
    assert apc.check_admission(_Req()) is None


def test_a_matching_patch_contract_is_admitted():
    from meshpipeline.application.approved_patch_contract import ApprovedPatchContract

    contract = ApprovedPatchContract.build([{"name": "body", "type": "wall"}],
                                           required=True).to_dict()
    assert apc.check_admission(
        _Req(approved_patch_contract=contract,
             intake_patches=[{"name": "body", "type": "wall"}])) is None


@pytest.mark.parametrize("delivered,what", [
    ([], "a dropped boundary"),
    ([{"name": "body", "type": "wall"}, {"name": "extra", "type": "wall"}], "an added boundary"),
    ([{"name": "renamed", "type": "wall"}], "a renamed boundary"),
    ([{"name": "body", "type": "symmetry"}], "a re-roled boundary"),
])
def test_an_altered_patch_set_is_refused(delivered, what):
    from meshpipeline.application.approved_patch_contract import ApprovedPatchContract

    contract = ApprovedPatchContract.build([{"name": "body", "type": "wall"}],
                                           required=True).to_dict()
    reason = apc.check_admission(_Req(approved_patch_contract=contract, intake_patches=delivered))
    assert reason, f"{what} was admitted"


def test_an_altered_intent_is_refused_by_the_fingerprint():
    from meshpipeline.application.dispatch_contract import recompute_intent_fingerprint

    req = _Req()
    good = recompute_intent_fingerprint({
        "mesh_engine": req.mesh_engine, "purpose": req.purpose, "input_kind": req.input_kind,
        "dimensionality": req.dimensionality, "intake_patches": req.intake_patches,
        "engine_params": req.engine_params,
        "requested_mesh_fidelity": req.requested_mesh_fidelity,
        "effective_mesh_fidelity": req.effective_mesh_fidelity,
        "mesh_fidelity_source": req.mesh_fidelity_source,
        "fidelity_policy_version": req.fidelity_policy_version,
        "request_txt": req.request_txt, "geometry_source": None,
        "geometry_interpretation": None})
    assert apc.check_admission(_Req(approved_intent_fingerprint=good)) is None
    # the SAME fingerprint against a different engine must be refused
    altered = apc.check_admission(_Req(approved_intent_fingerprint=good, mesh_engine="gmsh"))
    assert altered and "approved fingerprint" in altered


def test_the_admission_check_has_no_side_effect():
    # CODE only: the docstring says the function performs no publishing, which would otherwise
    # trip the scan on its own explanation.
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(apc.check_admission)))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and ast.get_docstring(node) is not None:
            node.body = node.body[1:]
    src = ast.unparse(tree)
    for effect in ("session", "commit", "publish", "record_dead_letter", "transition"):
        assert effect not in src, f"check_admission performs a side effect: {effect}"


# the durable refusal

class _S:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def commit(self): return None


class _Repo:
    def __init__(self): self.transitions = []; self.row = type("R", (), {"failed_reason": None})()
    async def transition(self, db, jid, status):
        from meshpipeline.persistence.job_state import TransitionResult
        self.transitions.append(status)
        return TransitionResult.applied
    async def get_internal(self, db, jid): return self.row


async def test_a_refused_run_is_durably_failed_and_reported(monkeypatch):
    seen = []
    import meshpipeline.errors as errs
    monkeypatch.setattr(errs, "record_dead_letter", lambda *a, **k: seen.append(a))
    published: list = []
    repo = _Repo()
    detail = await apc.refuse_admission(lambda: _S(), "intent altered", job_id=JOB,
                                        snapshot_id="snap-1", job_repo=repo, jlog=_Log(),
                                        publish=published.append)
    assert detail == {"job_id": JOB, "status": "failed", "reason": "patch_contract_mismatch"}
    assert JobStatus.failed in repo.transitions
    assert repo.row.failed_reason is not None, "no failure reason was recorded"
    assert seen, "the refusal produced no dead-letter record"
    assert published and "intent altered" not in published[0], (
        "the internal reason leaked into the user-facing closing")


async def test_a_refusal_survives_a_database_that_will_not_take_the_transition(monkeypatch):
    seen = []
    import meshpipeline.errors as errs
    monkeypatch.setattr(errs, "record_dead_letter", lambda *a, **k: seen.append(a))

    class _Boom(_Repo):
        async def transition(self, *a, **k): raise RuntimeError("db down")

    log = _Log()
    detail = await apc.refuse_admission(lambda: _S(), "bad", job_id=JOB, snapshot_id="s",
                                        job_repo=_Boom(), jlog=log, publish=lambda m: None)
    assert detail["status"] == "failed" and seen and log.warnings


# mutation guard

def test_the_orchestrator_no_longer_performs_the_gates_itself():
    import inspect

    import meshpipeline.application.pipeline_run as pr

    src = inspect.getsource(pr._run_async)
    assert "recompute_intent_fingerprint" not in src, "the run recomputes the fingerprint again"
    assert "assert_satisfied_by" not in src, "the run checks the patch contract again"
    assert "check_admission" in src and "refuse_admission" in src
