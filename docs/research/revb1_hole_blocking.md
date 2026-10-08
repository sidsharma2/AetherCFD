# Case study: REV B.1 probe, holes through tube walls (blocking design)

*AetherCFD copilot test, 2026-10-08. All work was done on sandbox copies in `D:\OpenFOAM\_aethercfd_scratch\revb1_probe`. The user's runs and generator were not modified.*

## The case

The REV B.1 sampling probe is modelled without the combustor. Sample gas enters at the tip and flows down a 2.159 mm bore. Dilution air flows back along the annulus between the inner and outer tubes. It enters the bore through 16 holes of 0.5 mm diameter, arranged as 8 rows of 2 at a 1.5 mm pitch with alternating 0/180 and 45/225 clocking.

The mesh is a native blockMesh dictionary produced by an analytic generator, `build_rev_b1_probe_interior_g1.py` (3,136 blocks), and split into fluid plus three solids. The fluid region of G1 has 733,824 cells.

## What was wrong (G1, measured with AetherCFD `mesh_hotspots`, checkMesh definitions)

| Defect | Where | Cause |
|---|---|---|
| Non-orthogonality 69.6°. All 45,120 faces above 50° lie in the domed hole caps | Under every hole, where the dilution jets enter the bore | The hole O-grid sits in a box that spans the whole 1.5 mm band axially but only 2 × 22.5° (0.49 mm) azimuthally at the cap. In this 3:1 box the O-grid's corner lines meet the box corners 27° off |
| Aspect ratio 72.5 and cell determinant 0.0024 | Segment E, the downstream bore | A single 1:8 grading over 100 cells makes a last cell of 1.12 mm |
| Face volume ratio 0.111, a 9:1 jump | x = 99.7325 mm | E's 1.12 mm cell meets segment F's 0.125 mm cells |

Run RS1a on G1 (rhoSimpleFoam, p relaxation 0.5) diverged at iteration 558: |U| reached 2,062 m/s and the continuity error reached 66. A later restart with p relaxation 0.1 (RS3r) converged.

## Variants (generator `build_rev_b1_probe_interior_g2.py`, `--variant`; `g1` reproduces G1 byte-for-byte)

| Variant | Change | Cells (fluid) | Non-ortho max | > 70° | AR | det min | vol. ratio min |
|---|---|---|---|---|---|---|---|
| G1 | — | 733,824 | 69.6° (caps) | 0 | 72.5 | 0.0024 | 0.111 |
| g2a | E multi-graded 0.125 → 0.30 → 0.125 mm (164 cells, ratio ≤ 1.15) | 881,280 | 69.6° (caps) | 0 | 19.2 | 0.0078 | 0.156 |
| g2b | + H-O hole box ±0.36 mm at all levels, graded end slabs | 1,028,736 | 76.5° (annulus) | 5,824 | 21.4 | 0.0005 | 0.124 |
| g2c | + box per level (cap 0.36, R0 0.40, R1–R3 0.45) | 1,028,736 | 70.2° | 64 | 19.2 | 0.0033 | 0.156 |
| g2d | g2b + elliptical layout at R2/R3 | 1,028,736 | 69.9° (annulus) | 0 | 19.2 | 0.0006 | 0.125 |
| g2f | g2d + O-grid graded hole→box (4×), end slab from 0.04 mm | 1,065,600 | 69.5° | 0 | 39.7 | 0.0001 | 0.156 |
| **g2h** | **g2c + elliptical layout at R2/R3** | **1,028,736** | **64.7°** | **0** | **19.2** | **0.0040** | **0.156** |

g2h is better than G1 on every metric:
- 0 faces above 65°, down from 3,456;
- the caps under the jets peak at 53.6°;
- no new size jumps.

## What the variants taught (now in `aethercfd.mesh.blocking` and the copilot tool `hole_box_design`)

1. **Corner misalignment predicts O-grid corner non-orthogonality.** The angle between the O-grid diagonal (45°, or atan(aspect) for an ellipse) and the box corner direction atan(w/a) is the predictor:
   - G1 cap: 27° off → 69.6°;
   - g2b annulus at R2: 26° off → 76.5°.
2. **Use the H-O topology for elongated boxes.** Shorten the O-grid box toward square and fill the rest of the band with H-type end slabs. The box length is bounded on both sides:
   - from below by the hole gap: about 0.44 r between hole and box, or the outer O-grid cells get squeezed;
   - from above by the end slab: it needs room for its cells.
3. **One box length does not fit every level.** On tubes the box half-width grows with radius (0.24 mm at the cap, 1.02 mm at R2):
   - with a single box length, fixing the cap broke the annulus (g2b);
   - per-level box lengths give gently conical slab faces, about 5°, with per-edge gradings (`edgeGrading`, consistent because the grading depends only on the level).
4. **Free levels can be elliptical.** Where the "hole" is not a physical surface (the annulus' outer wall R2 and beyond), an ellipse with the box's aspect points its diagonals at the box corners, and the O-grid morphs from the true circle at R1.
5. **Grading the O-grid toward the hole wall is not free.** In g2f/g2g it reduced the size jump against the end slab but produced thin, low-determinant cells (det down to 1e-4).
6. **blockMesh's merge check is relative to each block's span** (`1e-6 · span`, `blockMeshMergeTopological.C`). Face-projected faces shared by two blocks converge to points about 1e-6 mm apart. That passed with G1's 1.5 mm blocks and failed once the boxes were half as long. The fix was to keep the box-side quads' edges on the cylinder but not face-project them, as was already done for all plain fill faces.

## AetherCFD changes that came out of this test

- `script.run` command and copilot tool `run_python`: run a case-local generator script with approval, through the journal.
- `aethercfd-cli copilot …` subcommand: headless copilot.
- `constant/polyMesh/boundary` style files: entries inside root `N ( name { … } )` lists are addressable (`fluid_to_solid/type`), with lossless minimal edits.
- `log_note`: the closing FATAL of `checkMesh -region <r>` on a split mesh (v2306 mapped-patch lookup) is now explained to the copilot as benign.
- AetherMesh **Holes (O-grid)** tab and copilot tool `hole_box_design`.
- Packaging: CuPy needs the stdlib `graphlib`, which is imported only from Cython. Without it the packaged app silently ran every GPU feature on the CPU.
