# Responsibility: Describe the regions a multi-region case declares, and compare them with what was produced.
# Boundaries: the comparison fails in both directions: a declared region that is missing, and a region nobody declared.
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: Every file `checkMesh` and a solver need before a region is readable.
POLYMESH_COMPONENTS: tuple[str, ...] = ("boundary", "faces", "neighbour", "owner", "points")

#: `regions ( fluid (a b) solid (c) );` - OpenFOAM's own list-of-pairs form.
_REGIONS_BLOCK = re.compile(r"\bregions\s*\((.*?)\)\s*;", re.DOTALL)
_REGION_SET = re.compile(r"(\w+)\s*\(([^)]*)\)", re.DOTALL)


class RegionPropertiesError(ValueError):
    pass


@dataclass(frozen=True)
class DeliveredRegions:

    declared: dict[str, str] = field(default_factory=dict)   # region -> set name (fluid/solid)
    present: tuple[str, ...] = ()                            # complete, non-empty region meshes
    missing: tuple[str, ...] = ()                            # declared, no complete mesh
    undeclared: tuple[str, ...] = ()                         # meshed, never declared
    incomplete: dict[str, tuple[str, ...]] = field(default_factory=dict)   # region -> what is wrong

    @property
    def reconciled(self) -> bool:
        return bool(self.declared) and not self.missing and not self.undeclared

    def problem(self) -> str:
        if not self.declared:
            return "constant/regionProperties declares no regions"
        if self.missing:
            detail = "; ".join(
                f"{r} ({', '.join(self.incomplete[r])})" for r in self.missing
                if r in self.incomplete)
            return (f"declared region(s) {list(self.missing)} have no complete mesh"
                    + (f" - {detail}" if detail else ""))
        if self.undeclared:
            return (f"region(s) {list(self.undeclared)} were meshed but constant/regionProperties "
                    "never declared them")
        return ""


def parse_region_properties(text: str) -> dict[str, str]:
    block = _REGIONS_BLOCK.search(text or "")
    if block is None:
        raise RegionPropertiesError(
            "no `regions ( ... );` block - the file is truncated or not a regionProperties")
    out: dict[str, str] = {}
    for set_name, members in _REGION_SET.findall(block.group(1)):
        for region in members.split():
            out[region] = set_name
    return out


def read_region_properties(workspace) -> dict[str, str]:
    path = Path(workspace) / "constant" / "regionProperties"
    if not path.is_file():
        raise RegionPropertiesError("constant/regionProperties is missing - the region split "
                                    "did not complete")
    return parse_region_properties(path.read_text(errors="replace"))


def region_mesh_problems(workspace, region: str) -> tuple[str, ...]:
    poly = Path(workspace) / "constant" / region / "polyMesh"
    if not poly.is_dir():
        return ("no polyMesh directory",)
    problems: list[str] = []
    for component in POLYMESH_COMPONENTS:
        path = poly / component
        if not path.is_file():
            problems.append(f"missing {component}")
        elif path.stat().st_size == 0:
            problems.append(f"empty {component}")
    return tuple(problems)


def present_regions(workspace) -> tuple[str, ...]:
    const = Path(workspace) / "constant"
    if not const.is_dir():
        return ()
    return tuple(sorted(
        d.name for d in const.iterdir()
        if d.is_dir() and d.name != "polyMesh" and not region_mesh_problems(workspace, d.name)))


def inventory(workspace) -> DeliveredRegions:
    declared = read_region_properties(workspace)
    if not declared:
        raise RegionPropertiesError(
            "constant/regionProperties declares no regions - the split produced nothing")
    present = present_regions(workspace)
    incomplete = {}
    for region in declared:
        problems = region_mesh_problems(workspace, region)
        if problems:
            incomplete[region] = problems
    return DeliveredRegions(
        declared=declared,
        present=present,
        missing=tuple(r for r in declared if r not in present),
        undeclared=tuple(r for r in present if r not in declared),
        incomplete=incomplete)


__all__ = ["POLYMESH_COMPONENTS", "DeliveredRegions", "RegionPropertiesError", "inventory",
           "parse_region_properties", "present_regions", "read_region_properties",
           "region_mesh_problems"]
