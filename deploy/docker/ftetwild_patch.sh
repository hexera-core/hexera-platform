#!/bin/bash
# Responsibility: The pinned fTetWild source's own patches for FEA's last rung (LAD-fea-9, engines/fea_tetwild.py), each rewrite checked so a moved upstream line fails the build instead of shipping unpatched.
# Boundaries: run once in the fTetWild source root by the Dockerfile's ftetwild-build stage, after the configure step fetched its dependencies; it edits source files only and builds nothing.
# Collaborates with: Dockerfile (ftetwild-build), engines/fea_tetwild.py (BUILD, run_part: the stop file and the thread count), docs/RULES.md LAD-fea-9 / LAD-fea-18.
set -euo pipefail

# (1) REPEATABLE: upstream seeds two shuffles from std::random_device - fTetWild's triangle insertion order and
#     geogram's random_shuffle (the Delaunay's insertion order). Both become constants.
grep -q 'std::mt19937 g(rd());' src/TriangleInsertion.cpp
sed -i 's/std::mt19937 g(rd());/std::mt19937 g(1);/' src/TriangleInsertion.cpp
grep -q 'std::mt19937 g(1);' src/TriangleInsertion.cpp
grep -q 'std::mt19937 urng(rng());' build/_deps/geogram-src/src/lib/geogram/basic/algorithm.h
sed -i 's/std::mt19937 urng(rng());/std::mt19937 urng(1);/' build/_deps/geogram-src/src/lib/geogram/basic/algorithm.h
grep -q 'std::mt19937 urng(1);' build/_deps/geogram-src/src/lib/geogram/basic/algorithm.h

# (2) THE SAME MESH ON ANY NUMBER OF THREADS (LAD-fea-18): the tets a triangle may cut are gathered by TBB
#     threads into a concurrent vector in whatever order the threads finish, and that order is the cutting
#     order - taken in index order instead. The vertex smoothing splits its colour classes into parallel and
#     serial sets by a threshold of twice the thread count - a fixed threshold instead.
grep -q '^        for (int t_id : tbb_t_ids) {$' src/TriangleInsertion.cpp
sed -i 's|^        for (int t_id : tbb_t_ids) {$|        std::sort(tbb_t_ids.begin(), tbb_t_ids.end());\n        for (int t_id : tbb_t_ids) {|' src/TriangleInsertion.cpp
grep -q '^        t_ids.insert(t_ids.end(), tbb_t_ids.begin(), tbb_t_ids.end());$' src/TriangleInsertion.cpp
sed -i 's|^        t_ids.insert(t_ids.end(), tbb_t_ids.begin(), tbb_t_ids.end());$|        std::sort(tbb_t_ids.begin(), tbb_t_ids.end());\n        t_ids.insert(t_ids.end(), tbb_t_ids.begin(), tbb_t_ids.end());|' src/TriangleInsertion.cpp
test "$(grep -c 'std::sort(tbb_t_ids.begin(), tbb_t_ids.end());' src/TriangleInsertion.cpp)" = 2
grep -q 'mesh.one_ring_vertex_sets(mesh.params.num_threads \* 2, concurrent_sets, serial_set);' src/VertexSmoothing.cpp
sed -i 's|mesh.one_ring_vertex_sets(mesh.params.num_threads \* 2, concurrent_sets, serial_set);|mesh.one_ring_vertex_sets(64, concurrent_sets, serial_set);|' src/VertexSmoothing.cpp
grep -q 'mesh.one_ring_vertex_sets(64, concurrent_sets, serial_set);' src/VertexSmoothing.cpp

# (3) A STOP FILE (LAD-fea-18): when <output>.stop exists, the optimisation rounds and the sizing-field rounds
#     end where they are; fTetWild then finishes that mesh (the outside filtered) and writes it. The runner
#     writes the file when the rung's time is nearly spent.
grep -q '^#include <floattetwild/MeshImprovement.h>$' src/MeshImprovement.cpp
sed -i 's|^#include <floattetwild/MeshImprovement.h>$|#include <floattetwild/MeshImprovement.h>\n#include <fstream>\nstatic bool hexera_stop(const std::string\& out) { std::ifstream f(out + ".stop"); return f.good(); }|' src/MeshImprovement.cpp
grep -q '^    for (int it = 0; it < mesh.params.max_its; it++) {$' src/MeshImprovement.cpp
sed -i 's|^    for (int it = 0; it < mesh.params.max_its; it++) {$|    for (int it = 0; it < mesh.params.max_its; it++) {\n        if (hexera_stop(mesh.params.output_path)) { cout << "stop file: optimization ends at pass " << it << endl; break; }|' src/MeshImprovement.cpp
grep -q 'hexera_stop(mesh.params.output_path)) { cout' src/MeshImprovement.cpp
grep -q '^    int num_tets = mesh.get_t_num();$' src/MeshImprovement.cpp
sed -i '/^    int num_tets = mesh.get_t_num();$/{n;s|^    for (int i = 0; i < 20; i++) {$|    for (int i = 0; i < 20 \&\& !hexera_stop(mesh.params.output_path); i++) {|}' src/MeshImprovement.cpp
grep -q 'for (int i = 0; i < 20 && !hexera_stop(mesh.params.output_path); i++) {' src/MeshImprovement.cpp
echo "fTetWild patched: seeded, thread-stable, stop file"
