# Responsibility: Verify every view the reviewer takes reaches the live page, within a cap and without ever costing the review.
# Boundaries: the real node_reviewer over a fake renderer and a scripted model; the transport is a recording double or a fake Redis.
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re

import pytest
from tests.execution_publisher_double import RecordingExecutionPublisher

import meshpipeline.agents.reviewer.visual as visual
from meshpipeline.agents.reviewer.visual_surface import OpeningContext
from meshpipeline.contracts import model_inference as llm_router
from meshpipeline.contracts.review_evidence import EvidenceItem, InspectionTarget, TargetKind
from meshpipeline.engines.assurance import derive_assurance_plan
from meshpipeline.engines.registry import ENGINE_CATALOG
from meshpipeline.trace import policy as trace_policy

_GEOMETRY_SOURCE = {'source_id': '11111111-1111-4111-8111-111111111111', 'owner_id': 'o',
                    'object_key': 'sources/11111111-1111-4111-8111-111111111111',
                    'sha256': '1cf0557a718c367ab1cb8e34e7a86a191fbc5f1ff9f1b64f4466c8d2784ba4b8',
                    'size_bytes': 512, 'original_filename': 'wing.step', 'suffix_hint': '.step'}
_MANIFEST = {"mesh_units": "m", "patches": {"inlet": 1, "wall": 1},
             "quality": {"max_non_ortho": 10.0}}
_TARGETS = tuple(InspectionTarget(target_id=tid, kind=TargetKind.PATCH, label=tid, purpose="p",
                                  required=False) for tid in ("patch:inlet", "patch:wall"))
_OPENING = base64.b64encode(b"opening view").decode()
_EVIDENCE = re.compile(r"\[evidence:\s*([a-z]-\d{3})\]|^\s+([a-z]-\d{3}):", re.MULTILINE)


def _picture(n: int) -> str:
    return base64.b64encode(f"view {n}".encode()).decode()


class _Renderer:
    # Draws every known target - by default each view a different picture, or what `look` says
    # the n-th view of a target looks like - and refuses anything else in words.
    def __init__(self, look=None) -> None:
        self.drawn = 0
        self.look = look or (lambda n, tid: _picture(n))

    async def initial_context(self) -> OpeningContext:
        return OpeningContext(
            nav_context={"mesh_units": "m"}, patch_colour_legend="",
            initial_screenshot_b64=_OPENING, has_geometry=True,
            patch_names=[t.target_id for t in _TARGETS], inspection_targets=_TARGETS)

    async def execute_typed(self, fn, args):
        from meshpipeline.agents.reviewer.render_runtime import NOT_VIEWER_TOOL, TypedToolResult
        if fn == "submit_findings":
            return TypedToolResult(NOT_VIEWER_TOOL, None, False)
        tid = args.get("patch_name") or ""
        if tid not in {t.target_id for t in _TARGETS}:
            return TypedToolResult(f"Patch '{tid}' carries no review geometry.", None, True)
        self.drawn += 1
        frame = [{"type": "text", "text": f"Patch '{tid}' shown."},
                 {"type": "image_url",
                  "image_url": {"url": f"data:image/png;base64,{self.look(self.drawn, tid)}"}}]
        return TypedToolResult(frame, EvidenceItem(
            evidence_id="s", seq=self.drawn, label="l", purpose="p",
            image_ref=f"/views/{self.drawn}.png", covers_target=tid), True)


class _Model:
    # Each entry of `rounds` is one model turn of viewer calls; then it submits, citing all it saw.
    def __init__(self, rounds: list[list[str]]) -> None:
        self.rounds = [list(r) for r in rounds]
        self.axes = list(derive_assurance_plan(ENGINE_CATALOG["cfmesh"], "").axis_names)
        self.n = 0

    def _call(self, name: str, **args):
        from meshpipeline.contracts.model_inference import ToolCallRequest
        self.n += 1
        return ToolCallRequest(id=f"c{self.n}", name=name, arguments=json.dumps(args))

    async def __call__(self, *, messages, tools, job_id, user_id):
        from meshpipeline.contracts.model_inference import ModelRoundResult
        if self.rounds:
            calls = tuple(self._call("toggle_patch", patch_name=t) for t in self.rounds.pop(0))
            return ModelRoundResult(tool_calls=calls, finish_reason="tool_calls")
        seen: list[str] = []
        for m in messages:
            c = m.get("content")
            texts = [c] if isinstance(c, str) else [p.get("text", "") for p in c or []
                                                    if isinstance(p, dict)
                                                    and p.get("type") == "text"]
            for text in texts:
                seen += [a or b for a, b in _EVIDENCE.findall(text)]
        ids = list(dict.fromkeys(seen))
        findings = [{"axis_key": ax, "finding": f"{ax}: assessed", "evidence_ids": ids,
                     "passed": True} for ax in self.axes]
        return ModelRoundResult(tool_calls=(self._call(
            "submit_findings", rebuild_required=False, reasoning="done",
            axis_findings=findings),), finish_reason="tool_calls")


class _Publisher(RecordingExecutionPublisher):
    # The committed double, plus the picture itself - and, when asked, a transport that fails.
    def __init__(self, *, fail: bool = False, sink=None) -> None:
        super().__init__("e2e", "reviewer")
        self.fail, self.sink = fail, sink
        self.pictures: list[tuple[str, str]] = []

    async def ascreenshot(self, image_b64: str, op_id: str = "") -> None:
        self.pictures.append((op_id, image_b64))
        if self.fail:
            raise RuntimeError("redis is down")
        if self.sink is not None:
            self.sink.screenshot(image_b64, op_id=op_id)


def _review(monkeypatch, tmp_path, rounds, *, mode=trace_policy.RAW, publisher=None, rerun=0,
            look=None):
    publisher = publisher or _Publisher()
    saved: dict = {}
    monkeypatch.setattr(trace_policy, "current_mode", lambda: mode)

    @contextlib.asynccontextmanager
    async def _open(spec, ctx):
        yield _Renderer(look)

    monkeypatch.setattr(visual, "open_runtime", _open)
    monkeypatch.setattr(llm_router, "call_reviewer_with_tools", _Model(rounds))
    monkeypatch.setattr(visual, "save_review_artifacts",
                        lambda _dir, messages, *a, **k: saved.setdefault("messages", messages))
    monkeypatch.setattr(visual, "execution_publisher", lambda *a, **k: publisher)

    class _TL:
        def __init__(self, *a, **k): pass
        def log(self, *a, **k): pass
    monkeypatch.setattr("meshpipeline.capture.logger.TrainingLogger", _TL)

    out = asyncio.run(visual.node_reviewer({
        "job_id": "e2e", "engine": "cfmesh", "purpose": "", "openfoam_workspace": str(tmp_path),
        "mesh_manifest": _MANIFEST, "engine_params": {}, "retry_count": 0,
        "review_rerun_count": rerun, "geometry_source": _GEOMETRY_SOURCE,
        "request_txt": "mesh it", "review_brief_txt": "check it", "user_id": "u",
        "executor_success": True}))
    return out, publisher, saved.get("messages") or []


def _images_the_model_saw(messages) -> int:
    return sum(1 for m in messages if isinstance(m.get("content"), list)
               for p in m["content"] if isinstance(p, dict) and p.get("type") == "image_url")


# every view, each under its own id


def test_every_view_the_reviewer_takes_reaches_the_live_page(monkeypatch, tmp_path):
    out, pub, _ = _review(monkeypatch, tmp_path,
                          [["patch:inlet"], ["patch:wall"], ["patch:inlet", "patch:wall"]])
    assert out.get("reviewer_verdict") == "PASS", out
    assert pub.pictures == [("opening-render:0", _OPENING),
                            ("review-render:0:1", _picture(1)),
                            ("review-render:0:2", _picture(2)),
                            ("review-render:0:3", _picture(3)),
                            ("review-render:0:4", _picture(4))]


def test_a_rerun_names_its_pictures_apart_from_the_review_it_repeats(monkeypatch, tmp_path):
    _, pub, _ = _review(monkeypatch, tmp_path, [["patch:inlet"], ["patch:wall"]], rerun=1)
    assert [op for op, _ in pub.pictures] == ["opening-render:0:rerun1",
                                              "review-render:0:rerun1:1",
                                              "review-render:0:rerun1:2"]


def test_a_view_identical_to_one_already_shown_is_not_sent_again(monkeypatch, tmp_path):
    # The reviewer often returns to a camera it has used: the inlet always looks like the opening
    # view here, and the wall always looks the same. Only the wall's first view is new.
    def look(n, tid):
        return _OPENING if tid == "patch:inlet" else _picture(100)
    _, pub, _ = _review(monkeypatch, tmp_path,
                        [["patch:inlet"], ["patch:wall"], ["patch:inlet", "patch:wall"]],
                        look=look)
    assert pub.pictures == [("opening-render:0", _OPENING), ("review-render:0:1", _picture(100))]


def test_a_view_the_viewer_refused_puts_no_picture_on_the_page(monkeypatch, tmp_path):
    _, pub, _ = _review(monkeypatch, tmp_path,
                        [["patch:farfield"], ["patch:inlet"], ["patch:wall"]])
    assert [op for op, _ in pub.pictures] == ["opening-render:0", "review-render:0:1",
                                              "review-render:0:2"]


# the cap


def test_a_long_review_stops_publishing_at_the_cap_but_the_model_sees_every_view(
        monkeypatch, tmp_path):
    many = ["patch:inlet", "patch:wall"] * 15                  # thirty views in one turn
    out, pub, messages = _review(monkeypatch, tmp_path, [many])
    assert out.get("reviewer_verdict") == "PASS", out
    ops = [op for op, _ in pub.pictures]
    assert len(ops) == 1 + visual.LIVE_PICTURES_MAX == 25
    assert ops[-1] == f"review-render:0:{visual.LIVE_PICTURES_MAX}"
    assert len(set(ops)) == len(ops), "two pictures shared one id"
    assert _images_the_model_saw(messages) == 1 + 30, "the cap reached the model's own views"


# best effort


def test_a_failed_publish_never_fails_the_review(monkeypatch, tmp_path):
    out, pub, _ = _review(monkeypatch, tmp_path, [["patch:inlet"], ["patch:wall"]],
                          publisher=_Publisher(fail=True))
    assert out.get("reviewer_verdict") == "PASS", out
    assert not out.get("api_failure"), out
    # every picture was still tried - one failure does not silence the ones after it
    assert len(pub.pictures) == 3


@pytest.mark.parametrize("content", [
    "a refusal in words", None, [], [{"type": "image_url", "image_url": "not-a-dict"}],
    [{"type": "image_url", "image_url": {"url": "https://example.com/x.png"}}],
    [{"type": "image_url", "image_url": {"url": "data:image/png;base64,"}}],
])
def test_a_result_with_no_usable_picture_publishes_nothing_and_raises_nothing(content):
    pub = _Publisher()
    pictures = visual._LivePictures(pub, "0")
    asyncio.run(pictures(content))
    assert pub.pictures == [] and pictures.sent == 0


def test_a_lost_claim_still_stops_the_review(monkeypatch):
    # Best effort is for the transport. A worker that no longer owns the run must learn so here,
    # exactly as it does from the opening render and from every other reviewer event.
    from meshpipeline.contracts.event_stream import StaleExecutionPublish

    class _Superseded(_Publisher):
        async def ascreenshot(self, image_b64: str, op_id: str = "") -> None:
            raise StaleExecutionPublish("another worker owns this run")

    monkeypatch.setattr(trace_policy, "current_mode", lambda: trace_policy.RAW)
    frame = [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_picture(1)}"}}]
    with pytest.raises(StaleExecutionPublish):
        asyncio.run(visual._LivePictures(_Superseded(), "0")(frame))


def test_safe_mode_publishes_no_picture_at_all(monkeypatch, tmp_path):
    out, pub, _ = _review(monkeypatch, tmp_path, [["patch:inlet"], ["patch:wall"]],
                          mode=trace_policy.SAFE)
    assert out.get("reviewer_verdict") == "PASS", out
    assert pub.pictures == [], "a safe-mode deployment put a picture on the page"


# the durable log


class _FakeRedis:
    # The emit script's contract: an op key publishes once, the log keeps the light event, and
    # only a screenshot goes out live with more than the log holds.
    def __init__(self) -> None:
        self.seq = 0
        self.ops: set[str] = set()
        self.log: list[dict] = []
        self.live: list[dict] = []

    def eval(self, script, nkeys, *args):
        payload, _ttl, same, op_key = args[5], args[6], args[7], args[8]
        if op_key:
            if op_key in self.ops:
                return -1
            self.ops.add(op_key)
        self.seq += 1
        self.log.append({**json.loads(payload), "seq": self.seq})
        if same == "1":
            self.live.append(self.log[-1])
        return self.seq

    def publish(self, channel, payload):
        self.live.append(json.loads(payload))


def test_the_durable_log_keeps_no_picture_bytes(monkeypatch, tmp_path):
    from meshpipeline.adapters.event_stream.redis import JobPublisher

    sink = JobPublisher("e2e", "reviewer")
    sink._redis = redis = _FakeRedis()
    _review(monkeypatch, tmp_path, [["patch:inlet"], ["patch:wall"]],
            publisher=_Publisher(sink=sink))

    shots = [e for e in redis.log if e.get("type") == "screenshot"]
    assert len(shots) == 3, "three distinct pictures must be three logged events"
    assert all("image" not in e for e in shots), "picture bytes reached the replay log"
    live = [e for e in redis.live if e.get("type") == "screenshot"]
    assert [e["image"] for e in live] == [_OPENING, _picture(1), _picture(2)]
    assert [e["seq"] for e in live] == [e["seq"] for e in shots]

    # a retried publish of the same picture is the same event, not a second picture
    sink.screenshot(_picture(1), op_id="review-render:0:1")
    assert len([e for e in redis.log if e.get("type") == "screenshot"]) == 3
