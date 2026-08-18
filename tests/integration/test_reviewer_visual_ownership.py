# Responsibility: Verify every reviewer visual publication runs under the claimed execution ownership.
# Boundaries: the reviewer node's five publication sites and their refusal; the verdict logic is a unit contract.
from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

import pytest
from tests.integration import execution_ownership_support as ownership

from meshpipeline.application import execution_fence as fence
from meshpipeline.contracts.event_stream import StaleExecutionPublish

pytestmark = pytest.mark.asyncio
if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

VISUAL_MOD = "agents/reviewer/visual.py"
_TRANSPARENT = {"execution_publisher.py"}

#: cfMesh: two discovered patches and quality metrics that satisfy its declared criteria, so the
#: review can reach a verdict rather than stopping on missing evidence.
MANIFEST = {"mesh_units": "m", "patches": {"inlet": 1, "wall": 1},
            "quality": {"max_non_ortho": 10.0}}


# the two dependency boundaries below the branch under test
# The engine's RENDERER and the review PROVIDER. Both are existing seams the node already reaches
# through; no external provider may be contacted, and no publication path is replaced.


def _targets():
    from meshpipeline.contracts.review_evidence import InspectionTarget, TargetKind

    return tuple(InspectionTarget(target_id=tid, kind=TargetKind.PATCH, label=tid,
                                  purpose="inspect", required=False)
                 for tid in ("patch:inlet", "patch:wall"))


class _Runtime:
    def __init__(self, targets, *, screenshot: str):
        self._targets = targets
        self._ids = {t.target_id for t in targets}
        self._screenshot = screenshot

    async def initial_context(self):
        from meshpipeline.agents.reviewer.visual_surface import OpeningContext

        return OpeningContext(
            nav_context={"mesh_units": "m"}, patch_colour_legend="",
            initial_screenshot_b64=self._screenshot, has_geometry=True,
            patch_names=[t.target_id for t in self._targets],
            inspection_targets=self._targets)

    async def execute_typed(self, fn, args):
        from meshpipeline.agents.reviewer.render_runtime import NOT_VIEWER_TOOL, TypedToolResult
        from meshpipeline.contracts.review_evidence import EvidenceItem

        if fn == "submit_findings":
            return TypedToolResult(NOT_VIEWER_TOOL, None, False)
        tid = args.get("target_id") or args.get("patch_name") or ""
        frame = [{"type": "text", "text": "ok"},
                 {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]
        if tid in self._ids:
            return TypedToolResult(frame, EvidenceItem(
                evidence_id="s", seq=1, label="l", purpose="p", image_ref="/x.png",
                covers_target=tid), True)
        return TypedToolResult("no such target; nothing inspected", None, True)


def _open_runtime(targets, *, screenshot: str):
    @contextlib.asynccontextmanager
    async def _cm(spec, ctx):
        yield _Runtime(targets, screenshot=screenshot)
    return _cm


class _Provider:
    # Inspects every target, then submits one evidence-linked judgement per axis.
    def __init__(self, targets, axes, verdict: str = "PASS"):
        self.inspect = [t.target_id for t in targets]
        self.axes, self.verdict = list(axes), verdict

    async def __call__(self, *, messages, tools, job_id, user_id):
        import re

        from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest

        def call(name, **args):
            return ToolCallRequest(id=f"c-{name}", name=name, arguments=json.dumps(args))

        if self.inspect:
            return ModelRoundResult(tool_calls=(call("toggle_patch",
                                                     patch_name=self.inspect.pop(0)),),
                                    finish_reason="tool_calls")
        seen: list[str] = []
        for m in messages:
            content = m.get("content")
            parts = ([content] if isinstance(content, str)
                     else [p.get("text", "") for p in content or []
                           if isinstance(p, dict) and p.get("type") == "text"])
            for text in parts:
                seen += re.findall(r"\[evidence:\s*([a-z]-\d{3})\]", text)
                seen += re.findall(r"^\s+([a-z]-\d{3}):", text, re.MULTILINE)
        ids = list(dict.fromkeys(seen))
        findings = [{"axis_key": ax, "finding": f"{ax}: assessed", "evidence_ids": ids,
                     "passed": self.verdict != "FAIL"} for ax in self.axes]
        return ModelRoundResult(
            tool_calls=(call("submit_findings", rebuild_required=False, reasoning="done",
                             axis_findings=findings),),
            finish_reason="tool_calls")


# observation: the real adapter always runs, this only reads who called it


def _record(monkeypatch, seen: list) -> None:
    from meshpipeline.adapters.event_stream.redis import JobPublisher

    root = Path(sys.modules["meshpipeline"].__file__).parent

    def wrap(name: str):
        original = getattr(JobPublisher, name)

        def w(self, *a, **k):
            frame = sys._getframe(1)
            while frame is not None and Path(frame.f_code.co_filename).name in _TRANSPARENT:
                frame = frame.f_back
            try:
                rel = Path(frame.f_code.co_filename).resolve().relative_to(root).as_posix()
            except ValueError:
                rel = ""
            if rel == VISUAL_MOD:
                seen.append({"module": rel, "fn": frame.f_code.co_name, "method": name,
                             "op_id": k.get("op_id", ""), "impl": type(self).__name__,
                             "own": fence.current_ownership(), "args": a,
                             "job": str(getattr(self, "job_id", ""))})
            return original(self, *a, **k)

        monkeypatch.setattr(JobPublisher, name, w)

    for method in ("stage", "note", "warn", "screenshot", "verdict"):
        wrap(method)


def _disclosure(monkeypatch, mode: str) -> None:
    # The deployment's own answer, built by the REAL loader so raw mode is granted by its
    # two-switch acknowledgement rule and by nothing this test asserts. Safe mode reports the
    # render in words and publishes no image, so the screenshot site belongs to a deployment
    # that opted in.
    from meshpipeline.settings import modes as _modes
    from meshpipeline.settings import policy as _policy

    monkeypatch.setenv("PUBLIC_TRACE_MODE", mode)
    if mode == "raw":
        monkeypatch.setenv("ALLOW_PUBLIC_RAW_TRACE", "true")
    else:
        monkeypatch.delenv("ALLOW_PUBLIC_RAW_TRACE", raising=False)
    monkeypatch.setattr(_policy, "MODES", _modes.load_product_modes())


def _state(job_id, workspace: Path, *, executor_success: bool) -> dict:
    return {"job_id": str(job_id), "engine": "cfmesh", "purpose": "",
            "openfoam_workspace": str(workspace), "mesh_manifest": MANIFEST,
            "engine_params": {}, "retry_count": 0, "request_txt": "mesh it",
            "review_brief_txt": "check it", "user_id": "u",
            "executor_success": executor_success}


async def _review(monkeypatch, tmp_path, seen: list, *, executor_success: bool = True,
                  screenshot: str = "AAAA", disclosure: str = "raw"):
    import meshpipeline.agents.reviewer.visual as visual
    from meshpipeline.contracts import model_inference as llm_router
    from meshpipeline.engines.assurance import derive_assurance_plan
    from meshpipeline.engines.registry import ENGINE_CATALOG
    from meshpipeline.runtime.composition import install_adapters

    install_adapters()
    _disclosure(monkeypatch, disclosure)
    targets = _targets()
    monkeypatch.setattr(visual, "open_runtime", _open_runtime(targets, screenshot=screenshot))
    monkeypatch.setattr(llm_router, "call_reviewer_with_tools", _Provider(
        targets, derive_assurance_plan(ENGINE_CATALOG["cfmesh"], "").axis_names))

    job_id, own, Session, engine = await ownership.seeded_claim("review")
    _record(monkeypatch, seen)
    bound = own
    workspace = tmp_path / f"ws-{job_id.hex[:8]}"
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        with fence.execution_ownership(bound, session_factory=Session):
            out = await visual.node_reviewer(_state(job_id, workspace,
                                                    executor_success=executor_success))
        return {"job_id": job_id, "own": own, "out": out, "sessions": Session}
    finally:
        await engine.dispose()


# the accepted sites


@pytest.fixture()
async def verdict_run(monkeypatch, tmp_path):
    seen: list = []
    res = await _review(monkeypatch, tmp_path, seen)
    res["seen"] = seen
    yield res


@pytest.fixture()
async def nonverdict_run(monkeypatch, tmp_path):
    seen: list = []
    res = await _review(monkeypatch, tmp_path, seen, executor_success=False)
    res["seen"] = seen
    yield res


async def test_the_review_opens_under_the_claim_it_was_given(verdict_run):
    opening = [r for r in verdict_run["seen"] if r["fn"] == "node_reviewer"]
    assert [r["method"] for r in opening] == ["stage", "note"], \
        f"the reviewer opened with {[r['method'] for r in opening]}"
    for r in opening:
        assert r["impl"] == "JobPublisher"
        assert r["own"] is not None, "the reviewer opened with NO ownership bound"
        assert str(r["own"].job_id) == str(verdict_run["job_id"])
        assert r["own"].execution_generation == verdict_run["own"].execution_generation
        assert r["op_id"] == "inspect:0"


async def test_the_inspection_image_is_published_from_its_own_site(verdict_run):
    shots = [r for r in verdict_run["seen"] if r["method"] == "screenshot"]
    assert len(shots) == 1, f"the opening render published {len(shots)} images, not one"
    assert shots[0]["fn"] == "_publish_inspection_image", shots[0]["fn"]
    assert shots[0]["op_id"] == "opening-render:0"
    assert shots[0]["own"] is not None


async def test_a_safe_disclosure_deployment_publishes_no_inspection_image(monkeypatch, tmp_path):
    # The site is real, and so is the mode that closes it: the default deployment reports the
    # render in words and puts no image on the page. The review still reaches its verdict.
    seen: list = []
    res = await _review(monkeypatch, tmp_path, seen, disclosure="safe")
    assert res["out"].get("reviewer_verdict") == "PASS", res["out"]
    assert [r for r in seen if r["method"] == "screenshot"] == [], \
        "a safe-disclosure deployment put an inspection image on the page"


async def test_the_verdict_is_published_from_the_outcome_translator(verdict_run):
    verdicts = [r for r in verdict_run["seen"] if r["method"] == "verdict"]
    assert len(verdicts) == 1, f"the review published {len(verdicts)} verdicts"
    assert verdicts[0]["fn"] == "_translate_outcome", verdicts[0]["fn"]
    assert verdicts[0]["args"][0] == verdict_run["out"]["reviewer_verdict"] == "PASS"
    assert verdicts[0]["own"] is not None


async def test_a_non_verdict_warns_from_the_shared_refusal_site(nonverdict_run):
    warns = [r for r in nonverdict_run["seen"] if r["method"] == "warn"]
    assert len(warns) == 1, f"the non-verdict published {len(warns)} warnings"
    assert warns[0]["fn"] == "_nonverdict", warns[0]["fn"]
    assert warns[0]["op_id"] == "nonverdict:reviewer_evidence_missing:0"
    assert nonverdict_run["out"]["api_failure"] == "reviewer_evidence_missing"
    assert "verdict" not in [r["method"] for r in nonverdict_run["seen"]], \
        "an unvalidated execution produced a verdict"


async def test_the_success_path_completes_without_a_publisher_type_error(verdict_run):
    # The repaired contract, end to end: the node held the ownership-checked publisher, awaited
    # every publication through it, and returned a verdict. Before the repair this raised
    # `TypeError: publish must be the live publisher object` at the interaction inputs.
    out = verdict_run["out"]
    assert out.get("reviewer_verdict") == "PASS", out
    assert not out.get("api_failure"), out
    assert {r["method"] for r in verdict_run["seen"]} == {"stage", "note", "screenshot",
                                                          "verdict"}, verdict_run["seen"]


async def test_the_five_sites_are_five_distinct_production_functions(verdict_run,
                                                                     nonverdict_run):
    sites = {(r["fn"], r["method"]) for r in verdict_run["seen"] + nonverdict_run["seen"]}
    assert sites == {("node_reviewer", "stage"), ("node_reviewer", "note"),
                     ("_publish_inspection_image", "screenshot"),
                     ("_translate_outcome", "verdict"), ("_nonverdict", "warn")}, sorted(sites)


# the stale control


async def test_a_superseded_generation_reviews_nothing_and_writes_no_event(monkeypatch,
                                                                          tmp_path):
    from tests.integration.test_pipeline_node_ownership import _redis_state

    seen: list = []
    job_id = None
    before = after = None

    async def run():
        nonlocal job_id, before, after
        import meshpipeline.agents.reviewer.visual as visual
        from meshpipeline.contracts import model_inference as llm_router
        from meshpipeline.engines.assurance import derive_assurance_plan
        from meshpipeline.engines.registry import ENGINE_CATALOG
        from meshpipeline.runtime.composition import install_adapters

        install_adapters()
        _disclosure(monkeypatch, "raw")
        targets = _targets()
        monkeypatch.setattr(visual, "open_runtime", _open_runtime(targets, screenshot="AAAA"))
        monkeypatch.setattr(llm_router, "call_reviewer_with_tools", _Provider(
            targets, derive_assurance_plan(ENGINE_CATALOG["cfmesh"], "").axis_names))

        job_id, own, Session, engine = await ownership.seeded_claim("review-stale")
        _record(monkeypatch, seen)
        workspace = tmp_path / "stale-ws"
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            before = _redis_state(job_id)
            assert before["fence"], "the claim installed no fence to defend"
            with fence.execution_ownership(ownership.superseded(job_id, own),
                                           session_factory=Session):
                with pytest.raises(StaleExecutionPublish):
                    await visual.node_reviewer(_state(job_id, workspace, executor_success=True))
            after = _redis_state(job_id)
        finally:
            await engine.dispose()

    await run()
    assert seen == [], f"a superseded reviewer published {[(r['fn'], r['method']) for r in seen]}"
    assert after["seq"] == before["seq"], "a stale review advanced the event sequence"
    assert after["backlog"] == before["backlog"], "a stale review reached the durable backlog"
    assert after["opkeys"] == before["opkeys"], "a stale review recorded an operation key"
    assert after["fence"] == before["fence"], "a stale review disturbed the current fence"
