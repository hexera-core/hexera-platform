"""cfMesh conjugate: one manifold mesh of the whole block, split into coolant and metal.

The method, and why it is its own bundle rather than a capability on cfMesh: cfMesh meshes ONE
domain covering the block, metal included; topoSet tags the cells the coolant surface encloses;
splitMeshRegions -cellZonesOnly cuts two regions whose shared faces already existed, so the
interface is conformal by construction rather than by matching. cfMesh's own deliverable marker is
constant/polyMesh/owner and this writes constant/regionProperties, cfMesh is declared single-region
where a channelled plate's coolant is legitimately disconnected, and cfMesh is DEFAULT_ENGINE - so
none of this belongs in its spec.
"""
