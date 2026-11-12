# Responsibility: Bind Netgen to the runtime engine seam.
# Boundaries: a thin forwarder to this bundle's runner; it holds no mesher logic.
from __future__ import annotations


class ENGINE:
    name = "netgen"

    def __getattr__(self, item: str):
        from meshpipeline.engines.netgen import runner
        return getattr(runner, item)
