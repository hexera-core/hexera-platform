# Responsibility: Fail when an engine bundle's tessellation does not take every keyword the engine seam passes (engines/base.EngineBundle.tessellate_to_stl), and when the FEA mesher's payload leaves out the notes' requirements its adapter reads on the cloud mesher.
# Boundaries: signatures and constants only; nothing is meshed. Found on the real path (dev-areen, 11 Nov): Netgen stopped in staging with "unexpected keyword argument 'stage_timeout_s'", and a solid run with quality limits in the notes had no limit rows and an MMG pass with no targets.
from __future__ import annotations

import inspect

import pytest

from meshpipeline.engines.registry import engine_names
from meshpipeline.engines.runtime import get_engine

#: what cad/staging._prepare_cad passes to every bundle's tessellation (engines/base.EngineBundle.tessellate_to_stl)
SEAM_KEYWORDS = {"prepared": None, "stage_timeout_s": 60.0}


@pytest.mark.parametrize("engine", engine_names())
def test_every_engines_tessellation_takes_every_keyword_the_seam_passes(engine):
    fn = getattr(get_engine(engine), "tessellate_to_stl", None)
    if fn is None:
        pytest.skip(f"{engine} stages no surface")
    sig = inspect.signature(fn)
    try:
        sig.bind("in.step", "out.stl", **SEAM_KEYWORDS)
    except TypeError as exc:
        raise AssertionError(f"{engine}.tessellate_to_stl{sig} does not take the seam's keywords: {exc}") from None


def test_the_fea_payload_carries_the_notes_requirements_the_adapter_reads():
    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import mesh_requirements as MR

    assert MR.FILE in FD.PAYLOAD
