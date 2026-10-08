"""Root stream lists -- ``N ( name { ... } ... )`` (constant/polyMesh/boundary) -- addressable by name."""

from __future__ import annotations

from aethercfd.foamdict.edits import apply_edits, remove_entry, set_value
from aethercfd.foamdict.parser import parse
from aethercfd.foamdict.semantic import DictView

BOUNDARY = """FoamFile
{
    version     2.0;
    format      ascii;
    class       polyBoundaryMesh;
    object      boundary;
}

2
(
    inlet
    {
        type            patch;
        nFaces          576;
        startFace       2153952;
    }
    fluid_to_solid
    {
        type            mappedWall;
        inGroups        1(wall);
        nFaces          3072;
        startFace       2157984;
        sampleMode      nearestPatchFace;
        sampleRegion    solid;
        samplePatch     solid_to_fluid;
    }
)

// ************************************************************************* //
"""


def test_lookup_and_keys_inside_named_list_items():
    v = DictView(parse(BOUNDARY))
    assert v.get_text("inlet/nFaces").strip() == "576"
    assert v.get_text("fluid_to_solid/type").strip() == "mappedWall"
    assert v.keys("fluid_to_solid")[:2] == ["type", "inGroups"]
    assert v.get_text("missing/type") is None


def test_minimal_edits_inside_a_patch():
    doc = parse(BOUNDARY)
    out = apply_edits(BOUNDARY, set_value(doc, "fluid_to_solid/type", "wall"))
    for k in ("sampleMode", "sampleRegion", "samplePatch"):
        d = parse(out)
        out = apply_edits(out, remove_entry(d, f"fluid_to_solid/{k}"))
    assert "type            wall;" in out and "sample" not in out
    # everything else is byte-identical: the inlet patch, the header, the count and the trailing comment
    assert out.replace("""        type            wall;
        inGroups        1(wall);
        nFaces          3072;
        startFace       2157984;
""", "") == BOUNDARY.replace("""        type            mappedWall;
        inGroups        1(wall);
        nFaces          3072;
        startFace       2157984;
        sampleMode      nearestPatchFace;
        sampleRegion    solid;
        samplePatch     solid_to_fluid;
""", "")
    assert DictView(parse(out)).get_text("fluid_to_solid/type").strip() == "wall"
