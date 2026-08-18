# Responsibility: Verify a configure command is typed non-evidence, and cannot claim an inspection it did not make.
from __future__ import annotations

import pytest

from meshpipeline.contracts.review_evidence import (
    NON_RENDERING_COMMANDS,
    RENDERING_COMMANDS,
    CommandKind,
    EvidenceItem,
    InspectionTarget,
    RenderCapabilities,
    RenderCommand,
    RenderContext,
    ReviewRenderError,
    SessionUpdateResult,
    TargetKind,
)
from meshpipeline.sandbox.review_session import open_review_session


def _msh(p):
    p.write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    return p


def _png(p):
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 128)
    return p


class _Inner:

    def __init__(self, ws, *, misbehave=None):
        self.ws = ws
        self.closes = 0
        self.pan_step_mm = 10.0
        self.zoom_step = 1.5
        self._misbehave = misbehave

    def capabilities(self):
        return RenderCapabilities(
            views=(), targets=(InspectionTarget(target_id="patch:wall", kind=TargetKind.PATCH,
                                                label="wall", purpose="p"),),
            entities=("wall",),
            operations=frozenset({CommandKind.TOGGLE_ENTITY, CommandKind.CONFIGURE_SESSION}))

    def execute(self, command):
        if command.kind is CommandKind.CONFIGURE_SESSION:
            if self._misbehave == "configure_returns_evidence":
                return EvidenceItem(evidence_id="fake", seq=1, label="x", purpose="p",
                                    image_ref=str(_png(self.ws / "fake.png")),
                                    covers_target="patch:wall")
            if command.pan_step_mm is not None:
                self.pan_step_mm = command.pan_step_mm
            if command.zoom_step is not None:
                self.zoom_step = command.zoom_step
            return SessionUpdateResult(
                command_kind=command.kind.value,
                message=(f"Navigation defaults updated: pan_step={self.pan_step_mm:.0f} mm, "
                         f"zoom_step={self.zoom_step:.2f}×."),
                pan_step_mm=self.pan_step_mm, zoom_step=self.zoom_step)
        if self._misbehave == "render_returns_update":
            return SessionUpdateResult(command_kind=command.kind.value, message="pretend ok")
        return EvidenceItem(evidence_id="f1", seq=1, label="wall", purpose="p",
                            image_ref=str(_png(self.ws / "f1.png")), covers_target="patch:wall")

    def close(self):
        self.closes += 1


class _Spec:
    def __init__(self, inner):
        self._inner = inner
        self.render_artifacts = ()

    @property
    def review_renderer(self):
        inner = self._inner

        class _R:
            def required_artifacts(self): return ()
            def open(self, ctx, artifacts):
                inner.received_artifacts = dict(artifacts)
                return inner
        return _R()


def _ctx(ws):
    return RenderContext(workspace=str(ws), save_dir=str(ws), manifest={})


# 20: the complete inventory is accounted for
def test_every_reviewer_tool_is_classified_as_configure_or_rendering():
    from meshpipeline.agents.reviewer.render_runtime import (
        VIEWER_CONFIGURE_TOOLS,
        VIEWER_RENDERING_TOOLS,
    )
    from meshpipeline.agents.reviewer.tools import REVIEWER_TOOLS

    names = {t["function"]["name"] for t in REVIEWER_TOOLS}
    unclassified = names - VIEWER_CONFIGURE_TOOLS - VIEWER_RENDERING_TOOLS
    assert not unclassified, (
        f"reviewer tools the runtime cannot route: {sorted(unclassified)}")
    assert VIEWER_CONFIGURE_TOOLS & names, "no configure tool - navigation cannot be set"
    assert VIEWER_RENDERING_TOOLS & names, "no rendering tool - the review sees nothing"
    assert not (VIEWER_CONFIGURE_TOOLS & VIEWER_RENDERING_TOOLS), \
        "a tool is both configure and rendering - the runtime would route it twice"


def test_the_command_vocabulary_partitions_into_rendering_and_not():
    assert RENDERING_COMMANDS | NON_RENDERING_COMMANDS == set(CommandKind)
    assert RENDERING_COMMANDS & NON_RENDERING_COMMANDS == frozenset()
    assert NON_RENDERING_COMMANDS == {CommandKind.CONFIGURE_SESSION}


# 6-10: the result type cannot claim inspection
def test_the_update_result_has_no_field_that_could_claim_inspection():
    import dataclasses

    fields = {f.name for f in dataclasses.fields(SessionUpdateResult)}
    for forbidden in ("image_ref", "patches", "regions", "groups", "axis_ids", "covers_target"):
        assert forbidden not in fields, f"SessionUpdateResult can claim {forbidden}"


async def test_configure_returns_a_typed_non_evidence_result(tmp_path):
    inner = _Inner(tmp_path)
    async with open_review_session(_Spec(inner), _ctx(tmp_path)) as s:
        r = s.execute(RenderCommand(kind=CommandKind.CONFIGURE_SESSION,
                                    pan_step_mm=25.0, zoom_step=2.0))
    assert isinstance(r, SessionUpdateResult)
    assert not isinstance(r, EvidenceItem)


async def test_the_result_text_is_byte_compatible_with_today(tmp_path):
    inner = _Inner(tmp_path)
    async with open_review_session(_Spec(inner), _ctx(tmp_path)) as s:
        r = s.execute(RenderCommand(kind=CommandKind.CONFIGURE_SESSION,
                                    pan_step_mm=25.0, zoom_step=2.0))
    assert r.message == "Navigation defaults updated: pan_step=25 mm, zoom_step=2.00×."


async def test_the_settings_affect_subsequent_navigation(tmp_path):
    inner = _Inner(tmp_path)
    async with open_review_session(_Spec(inner), _ctx(tmp_path)) as s:
        s.execute(RenderCommand(kind=CommandKind.CONFIGURE_SESSION, pan_step_mm=42.0))
        assert inner.pan_step_mm == 42.0
        s.execute(RenderCommand(kind=CommandKind.CONFIGURE_SESSION, zoom_step=3.0))
        assert inner.zoom_step == 3.0 and inner.pan_step_mm == 42.0   # partial update


# 12-15: neither category can impersonate the other
async def test_a_successful_configure_is_not_read_as_a_render_failure(tmp_path):
    inner = _Inner(tmp_path)
    async with open_review_session(_Spec(inner), _ctx(tmp_path)) as s:
        r = s.execute(RenderCommand(kind=CommandKind.CONFIGURE_SESSION, pan_step_mm=5.0))
    assert r.diagnostics == ()
    assert not any("rejected" in d for d in r.diagnostics)


async def test_a_rendering_command_cannot_return_a_session_update(tmp_path):
    inner = _Inner(tmp_path, misbehave="render_returns_update")
    async with open_review_session(_Spec(inner), _ctx(tmp_path)) as s:
        with pytest.raises(ReviewRenderError, match="must be reported as failed evidence"):
            s.execute(RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id="wall"))


async def test_a_configure_cannot_fabricate_evidence(tmp_path):
    inner = _Inner(tmp_path, misbehave="configure_returns_evidence")
    async with open_review_session(_Spec(inner), _ctx(tmp_path)) as s:
        with pytest.raises(ReviewRenderError, match="must never claim inspection"):
            s.execute(RenderCommand(kind=CommandKind.CONFIGURE_SESSION, pan_step_mm=1.0))


async def test_a_blank_image_from_a_real_render_is_still_rejected(tmp_path):
    class _Blank(_Inner):
        def execute(self, command):
            return EvidenceItem(evidence_id="b", seq=1, label="w", purpose="p",
                                image_ref="", covers_target="patch:wall")

    inner = _Blank(tmp_path)
    async with open_review_session(_Spec(inner), _ctx(tmp_path)) as s:
        frame = s.execute(RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id="wall"))
    assert frame.image_ref == "" and not frame.rendered


# 16-17: lifecycle
async def test_configure_fails_after_close_like_every_other_command(tmp_path):
    inner = _Inner(tmp_path)
    async with open_review_session(_Spec(inner), _ctx(tmp_path)) as s:
        s.close()
        with pytest.raises(ReviewRenderError, match="closed"):
            s.execute(RenderCommand(kind=CommandKind.CONFIGURE_SESSION, pan_step_mm=1.0))


async def test_an_exception_during_configure_still_closes_the_session(tmp_path):
    class _Boom(_Inner):
        def execute(self, command):
            raise RuntimeError("configure blew up")

    inner = _Boom(tmp_path)
    with pytest.raises(RuntimeError):
        async with open_review_session(_Spec(inner), _ctx(tmp_path)) as s:
            s.execute(RenderCommand(kind=CommandKind.CONFIGURE_SESSION, pan_step_mm=1.0))
    assert inner.closes == 1


# typed settings stay closed
def test_the_settings_vocabulary_is_closed_named_fields_not_a_dict():
    import dataclasses

    fields = {f.name: f.type for f in dataclasses.fields(RenderCommand)}
    assert "pan_step_mm" in fields and "zoom_step" in fields
    for f in fields.values():
        assert "dict" not in str(f).lower(), "RenderCommand carries a settings dict"


def test_the_external_tool_schema_is_untouched():
    from meshpipeline.agents.reviewer.tools import REVIEWER_TOOLS

    tool = next(t["function"] for t in REVIEWER_TOOLS
                if t["function"]["name"] == "set_navigation_defaults")
    props = tool["parameters"]["properties"]
    assert set(props) == {"pan_step_mm", "zoom_step"}
    assert tool["parameters"].get("required", []) == []
