# AetherMesh: automatic and geometry-aware meshing — research memo

*AetherCFD Studio, meshing module. Written 2026-10-07. Status: research input for design. It is not a decision record.*

**Citation conventions.** Bracketed tags such as [E8] or [O3] refer to the reference list at the end. **[LOCAL]** marks a fact I checked directly on 2026-10-07 in the development WSL distro (ESI v2306 package build `_cafbeeb2-20260127`, OpenFOAM-14 build `14-b4f91ad8bbba`) or in the AetherCFD venv. Paragraphs that begin **Inference:** are my reasoning from the cited evidence. They are not vendor statements.

---

## 0. Key findings

- Ennova is a small Berkeley company (founded 2007) run by Michael Hohmeyer, a co-founder of ICEM CFD. Every product is licensed. There is no free or community tier. List prices run from $36k perpetual or $1.2k per month up to $60k ("Ultra"), plus a $3k/yr academic seat [E2][E10]. What is free: the user guide, tutorials, release notes, sample data, and one benchmark deck [E12][E14].
- The public material describes the algorithms in substance. Repair runs on a **topology layer** that sits over untouched CAD and merges the smallest-distance defects first, with no user tolerance. **Prisms grow by advancing layers** until the cells are isotropic. Sizing comes from **curvature (sag), gap counts and growth ratio**. A **Delaunay tet mesh is dualised** into "conformal" polyhedra. An **octree shrink-wrap** handles dirty CAD. The goal is "hex where it matters" (H+), with blocks placed from CAD parameter directions [E8][E13][E14][E15][E17][E23].
- Everything Ennova sells can be approximated with our stack: snappyHexMesh (both forks), the cfMesh `cartesianMesh`/`pMesh` binaries (present in our v2306 package), `polyDualMesh`, gmsh and Python geometry analysis [LOCAL]. The differentiators are a sound **size-field engine**, **geometry recognition**, a **y+-driven layer planner**, and a **check–diagnose–adjust loop**. Better meshers are not required.

---

## 1. Ennova

### 1.1 Company and products

Ennova Technologies was founded in 2007 and is headquartered in Berkeley. Its "roots" are in ICEM CFD and Ansys-era meshing [E2]. Michael Hohmeyer (CEO) co-founded ICEM CFD. IDAJ is a co-founder, a major investor and the exclusive Far-East distributor [E2]. The company's 2024 benchmark deck describes Hohmeyer as "founder and core developer of ICEM CFD" [E14].

Products [E3][E4][E26]:
- **EnnovaCFD for OpenFOAM**, the flagship. It is a pre/post-processor around a separately installed OpenFOAM, with a GUI that writes the dictionaries. It supports both the Foundation and ESI/OpenCFD forks and runs cases locally, remotely or through WSL [E5].
- **Ennova Blue**, a Windows-native bundle built on blueCFD-Core that needs no Linux or WSL [E4][E13].
- **Ennova Marine**, for hydrodynamics. Its meshing stack is the same [E26].
- **OEM/Source licences**: Ennova can be resold off the shelf, re-skinned as a custom GUI, or licensed as component source code [E27].
- **Application tools**, namely an automotive wind-tunnel tool and an environmental-assessment tool [E3].

### 1.2 Free versus paywalled

| Free / open | Paywalled |
|---|---|
| User guide (Dr.Explain HTML) [E15]; tutorial PDF, sample geometry, and an OpenFOAM case [E12] | All software functionality. Installers are emailed on request, and running them requires a node-locked `ennova.lic` (tied to the MAC address) or an RLM floating licence [E11][E12] |
| Release notes 1.11.2, July 2026 [E13]; AutoCFD4 polyhedral-vs-hex-core deck [E14] | EnnovaCFD Standard $36k perpetual; for OpenFOAM or Foundation $36k / $12k yr / $1.2k mo; "FOAM" $39k / $13k / $1.3k, academic $3k yr; **Ultra $60k / $20k yr** [E10] |
| OpenFOAM itself (free on any number of cores) [E5] | OEM source licence, price on request [E27] |

The price page lists no features per tier, so it does not say what "Ultra" adds [E10].

### 1.3 Advertised meshing features

| Area | What is advertised |
|---|---|
| Import | HOOPS Exchange, covering about 40 formats including CATIA, NX, Parasolid, STEP and STL [E25] |
| Repair | Automatic, parallel, "99% of the model without manual input". Stitch and Intersect, gap and overlap repair. Geometry stays native and xyz is never changed [E7][E9][E13] |
| Wrapping | Octree shrink-wrap for "very dirty" CAD. Parameters include gap-close tolerance and refinement, which volume to keep (external, largest internal, or seed point), keep-internal-shells, and isolated-island deletion. This is followed by **Remesh** to "standardize" the arbitrary-growth wrap [E6][E17][E19][E20] |
| Surface | Topology-based "body fitted anisotropic surface mesh", with structured quads allowed ("All Triangles" disables them) and a max aspect ratio for stretched quads on highly curved faces [E6][E17] |
| Volume | Topology (hybrid structured/unstructured), Cartesian octree, cut-mesh, isotropic tet, and **dualizer** "honeycomb" polyhedra. Everything runs in parallel [E6][E9][E4] |
| Boundary layers | Prism mesher for both wrapped and topology meshes. Settings: number of layers, first height and ratio (global or per face group), **Auto Reduction** of heights where prisms are blocked, and a **Minimum Quality** threshold below which no layer is extruded [E6][E17] |
| Sizing | Max and min size, **sag number** (chord deviation divided by element size), **refinement number** (elements per circle, tied to sag), **size ratio** (growth), **elements in gap** with a search floor, density regions (cylinder, box or sphere built from an edge, face or vertex), periodic surfaces, "Create Thin Cut", and leak detection [E17][E22][E28][E21] |
| Smoothing | "Variational" volume smoother ("Booster"), which "will never make a mesh worse" [E6][E18] |
| Automation | Scripting: JavaScript-like in the older guide, Python in Ennova Blue. User scripts and text-driven GUI specs [E16][E4][E29] |
| AI | **None advertised** on any page reviewed [E1][E3][E4][E13] |
| Export | OpenFOAM (both forks), Fluent, CGNS, Kestrel (.avm), UGrid, Star-CD and others [E24] |

### 1.4 Public algorithm descriptions

1. **Pipeline.** Imported CAD becomes a distributed virtual assembly. "Using many tests" it is reduced to N closed volumes, and sizing is applied "simultaneously based on these tests". Surface size is deduced "based on local curvature, growth rates" and solver-dependent aspect-ratio limits. Prisms are "grown in an advancing frontal manner until they become isotropic", and an advancing front fills the rest [E15].
2. **Mesh topology layer.** The topology is connectivity plus pointers to unchanged CAD, "similar to the blocking in ICEM HEXA but much more general". Intersections are stored as symbolic links to the two parent surfaces, and mesh points on them come from solving the nonlinear equations [E8].
3. **Tolerance-free repair.** Candidate operations (merge nodes, edges or faces; split edges) each carry a distance. They are sorted, the smallest are applied first, and repair stops wherever the model is locally watertight. Repair is "controlled by the meshing algorithm" and only cleans what it must mesh [E8][E7].
4. **Anisotropic and swept regions.** Leading edges get "fully anisotropic structured" cells. Narrow gaps (brake pad and rotor, blade tip, valve seat) get swept meshes whose in-plane size is many times the gap width. Each choice saves about an order of magnitude in cell count. Where these regions go is read from CAD parameter directions, which designers align with principal curvature [E8]. The H+ mesher of 2018 builds "hybrid mesh with unstructured regions away from the geometry and structured, all hex regions in critical areas", with y+-correct thin surface cells [E23].
5. **Polyhedra.** The polyhedral mesher is a "Tetra-Polyhedral volume meshing tool that uses the Delaunay method", with polyhedral cell counts at "approximately 1/6th" of the tet mesh [E14]. Version 1.11.2 improved the "tetrahedral volume mesh generation used during polyhedral mesh creation", "dualization near folded surfaces", proximity refinement, growth control and refinement transitions [E13]. On DrivAer notchback, a 20M-cell polyhedral mesh with k-ω SST gave Cd 0.277 against 0.255 measured [E14].
6. **Wrapping and leaks.** A shrink-wrap is generated by an octree approach, after which the volume mesh is discarded and only the surface is kept [E19]. Leak detection needs a source and a target: if there are no leaks, no path joins them [E21].

The patents I found that cover comparable features belong to **other** companies. One is an MSC/Hexagon octree shrink-wrap: build the octree, extract the outer skin, project vertices to the nearest point, reproject when the projection angle exceeds 60° or the rotation exceeds 175°, then collapse and swap away folds [P1]. The other is an Ansys patent that fills a gap by dualising a surface mesh into conformal polyhedra [P2]. I found no Ennova patents or papers.

### 1.5 What the paywalled features probably do

- **Inference (topology / H+ mesher).** The mesher classifies faces by principal-curvature anisotropy (κ₁/κ₂) and by the alignment of their parameter lines [E8]. On strongly anisotropic, well-parameterised faces (leading and trailing edges, fillets) it builds a mapped quad surface mesh, stretched up to the "Max Aspect Ratio" [E17], and extrudes structured hex stacks off it. Elsewhere it uses a triangle surface with prisms and tets. In effect this is ICEM-HEXA blocking with the decomposition automated from CAD intent. It is the most likely content of the "Ultra" tier, but nothing published confirms that [E10].
- **Inference (swept gaps).** "Create Thin Cut" [E28] and "Elements in gap" [E17] point to proximity detection between pairs of opposite faces. Where the gap g is much smaller than the local size h, one face is meshed, projected onto the other, and joined by a few prism or hex layers. This is the "swept mesh" of [E8].
- **Inference (dualizer).** The sequence is: prism layers first [E15], then a Delaunay tet fill, then a vertex (median) dual. Prism stacks keep matching polygonal caps, which is what "conformal" means here [E13]. The 1/6 cell-count claim [E14] fits a vertex dual, because an isotropic Delaunay mesh has roughly 5–6 tets per vertex (my estimate). The DrivAer cut-planes [E14] also show Cartesian hex in the wake box and on the floor. That suggests a poly + hex-core hybrid.
- **Inference (Booster).** "Never makes a mesh worse" [E18] reads as local, optimisation-based node relocation with an accept-only-if-the-worst-quality-improves test. It matches the "smart" or optimisation smoothing of Freitag and Knupp (§2.7).
- **Inference (prism robustness).** "Auto Reduction" and "Minimum Quality" [E17] amount to collision-aware height scaling plus local layer removal. This is the same strategy snappyHexMesh uses (§2.6).
- **Inference (sag ↔ refinement number).** The guide says the two are tied by a formula [E17]. For chord deviation δ on radius R, δ ≈ h²/(8R). So the sag number s = δ/h = h/(8R), and the number of elements per circle is N = 2πR/h = π/(4s).

---

## 2. State of the art (survey)

### 2.1 Sizing fields

The target size h(x) is the minimum of several components:
- **Curvature.** `h_κ = 2π/(N·κ_max)`, where N is the number of elements per full turn. This is gmsh's `Mesh.MeshSizeFromCurvature` [G1]. Equivalently, h is limited by sag (above).
- **Proximity, or local feature size.** The local feature size is the distance to the medial axis [L3]. A gap of width g = 2·r_ins (the inscribed-sphere radius) gets `h_g = g/n_g`. In Cubit, skeleton sizing computes 3D/2D/1D proximity and curvature from a wave-front skeleton on an octree [L4]. Persson defines feature size through the medial-axis transform, computed from a distance function [L1]. The local feature size vanishes at sharp edges and corners, so sizing must floor it [L5].
- **Features and user regions.** Fixed sizes near feature curves, in boxes, or in wakes.
- **Gradation.** h must be Lipschitz: `|∇h| ≤ g = β−1` for a growth ratio β. The minimal admissible field is the envelope `h(x) = min_y [h₀(y) + g·|x−y|]`. Persson's gradient-limiting equation computes it with Hamilton–Jacobi solvers [L1]. On a graph, a Dijkstra sweep `h_i ← min(h_i, h_j + g·|x_i−x_j|)` does the job. Borouchaki et al. treat it as metric reduction [L2].
- **Storage.** Store h on an octree independent of the mesher, as Bawin et al. do. Any mesher that honours a size field can then use it, and their method needs only five parameters across the ABC dataset [G3].

### 2.2 Feature detection and preservation

For each manifold edge, the dihedral deviation is `θ_e = arccos(n₁·n₂)`. An edge is a feature when θ_e > θ_f, typically 30–45° [Y3]. Boundary edges (one face) and non-manifold edges (three or more faces) are always features [Y3]. Feature edges are then chained into curves, split at corners where the valence ≠ 2 or the turning angle is large, and chains shorter than a length or element threshold are dropped (`minLen`/`minElem`) [O12].

OpenFOAM states the same test as an included angle: edges whose adjacent normals form an angle below `includedAngle` are features [O12]. So **includedAngle = 180° − θ_f**.

Features are preserved in three ways:
- by **conformity**: gmsh `classifySurfaces(angle, …, curveAngle)` then `createGeometry` produces discrete curves that the mesher respects [G1];
- by **snapping**: snappyHexMesh moves vertices onto an edge mesh (explicit) or infers features from the surface (implicit). A wrong edge mesh produces bad edges, so it must be checked visually [O9];
- by **refinement**: refine near features to a chosen `features` level [O5].

### 2.3 Geometry recognition for strategy selection

- **Sweepable or extruded regions.** Cubit's automatic scheme selection finds sweepable volumes and their source and target faces. Sweeping needs the same number of layers along every path [L9].
- **Axisymmetric bodies.** trimesh reports `symmetry ∈ {radial, spherical}` and a `symmetry_axis` (the axis a profile was revolved around) [Y2].
- **Thin walls and gaps.** Measure them with inscribed-sphere thickness (`trimesh.proximity.thickness(..., method="max_sphere")`) [Y1].
- **Pipes and annuli.** Use an O-grid ("butterfly") topology: a square core block joined to curved outer blocks. It avoids the degenerate centreline, and its known weakness is an abrupt jump in non-orthogonality at the core interface [O10].
- **Medial-axis decomposition.** Tam and Armstrong split a 2D domain at medial-axis nodes into polygons with at most 6 sides, mesh them by midpoint subdivision, and reconcile interval counts by integer programming [L6]. The 3D medial-surface extension handles convex, flat and concave edges [L6]. Critics note fine subdivisions (72 elements for a brick) and poor elements at degeneracies [L7].

### 2.4 Cross-, frame-field and polycube hex meshing (brief)

- **Cross fields (2D/surfaces): practical.** Gmsh's quasi-structured quad pipeline places points frontally along a cross field and size map, recombines, then removes irregular vertices that are not singularities. It keeps validity by reverting edits that hurt quality [G2].
- **Frame fields and integer-grid maps (3D): research grade.** The 2022 hex survey reviews polycubes, block decomposition, dual sheets and frame fields, and catalogues their "unsolved challenges", including relaxed hex-dominant outputs [L8]. Ennova's founder calls fully automatic hex/multiblock generation unsolved [E8].
- **Polycubes: robust for blobby organic shapes.** The PolyCube structure and volumetric map are computed jointly [L10]. The approach is ill-suited to CAD with thin features. **Verdict:** use cross-field quads for surface and 2D profile meshes. Defer 3D hex beyond templates.

### 2.5 Octree, cut-cell and snap-to-surface (snappyHexMesh)

The algorithm runs in three stages: castellate (refine a background hex mesh near surfaces, features and regions, then keep the cells reachable from `locationInMesh`), snap (move boundary vertices onto the surface and relax), and optionally add layers (shrink the mesh and insert hex layers) [O5]. After every iteration snappyHexMesh checks quality and *partially reverts* bad changes. That keeps the mesh valid, but it makes the process "noisy": small setting changes ripple through the whole mesh [O9].

Known failure modes:
1. Layer coverage collapses, because edges are collapsed aggressively and coverage decays over the iterations [O9].
2. Twisted boundary faces and non-manifold points produce bad layer cells [O9].
3. A wrong feature edge mesh gives bad edge snapping [O9].
4. Leaks through gaps let the mesher reach outside regions. ESI adds `locationsOutsideMesh`, `useLeakClosure` and `leakLevel` [O4].
5. Small gaps are under-resolved. ESI adds `gapLevel` and `blockLevel` [O4].
6. There are 50+ non-geometric parameters [O9].
7. High aspect ratios are only available through directional `levelIncrement` [O4].

foamyHexMesh produces Voronoi duals of an iterated Delaunay mesh with very regular cells. It requires a perfectly closed surface and cannot reliably reach high aspect ratios [O7].

### 2.6 Boundary-layer extrusion

- **Generalized advancing layers.** These handle non-manifold topology in automotive-scale models [L11].
- **Normals.** Normals at ridges and corners need multiple point normals with degenerate faces, plus global smoothing, because a point normal that suits each face can still be bad for its prism [L12]. The "most normal" normal line continues through to entropy-satisfying treatment of concave corners [L12]. A robust BL method appears in the IMR 2012 proceedings [L13].
- **Collisions.** Cap total thickness T relative to the medial distance. snappyHexMesh uses `maxThicknessToMedialRatio 0.3` and `minMedialAxisAngle 90` [O4].
- **Concave corners.** Collapse or terminate layers. In ESI this is `layerTerminationAngle`, which helps stability [O9][O4].
- **First cell from y+.** The calculation chain [L14]:
  - `Re_x = ρUx/μ`
  - `C_f = (2·log₁₀Re_x − 0.65)^−2.3`, Schlichting, valid for Re_x < 10⁹
  - `τ_w = ½C_fρU²`
  - `u_τ = √(τ_w/ρ)`
  - `y_p = y⁺μ/(ρu_τ)`
  
  A cell-centred FV code evaluates y⁺ at the cell centre, so the **first cell height is Δy₁ = 2·y_p** (some users stress this point [L14]). Total thickness is `T = Δy₁(rᴺ−1)/(r−1)`.

### 2.7 Smoothing, untangling, target matrices

- **Laplacian smoothing** is cheap but can invert elements. The "smart" variant accepts only moves that improve quality [L15].
- **Optimisation-based smoothing** (for example, maximising the minimum dihedral angle) beats Laplacian smoothing [L15].
- **Untangling.** Standard barrier objectives are undefined for inverted elements. Escobar et al. modify them so they are smooth everywhere, which untangles and smooths in one pass (GPL code available) [L16].
- **Mesquite** chooses a metric, an objective template and a solver for node relocation [L17]. The **target-matrix paradigm** prescribes a desired Jacobian W at sample points and optimises μ(A·W⁻¹). It was later extended to high-order meshes [L18].

### 2.8 Quality-driven loops

NASA's CFD Vision 2030 calls meshing a persistent bottleneck that needs human intervention because of poor robustness [L19]. Effective systems close the loop at three levels:
- *inside* the mesher (snappyHexMesh's undo iterations [O9]);
- *around* it, by sweeping snappy parameters and scoring checkMesh results, layer coverage and a 200-iteration solver stability run (the "snappyLayerTests" study found a tunable trade-off) [O9];
- *before* it, with repair driven by what the mesher needs [E7].

---

## 3. Mapping to tools we can run

### 3.1 Verified inventory [LOCAL]

| Fork / tool | Relevant executables |
|---|---|
| ESI v2306 package | `blockMesh snappyHexMesh surfaceFeatureExtract extrudeMesh refineMesh topoSet checkMesh foamyHexMesh foamyQuadMesh polyDualMesh collapseEdges createBaffles` + **cfMesh**: `cartesianMesh cartesian2DMesh tetMesh pMesh generateBoundaryLayers improveMeshQuality surfaceFeatureEdges` |
| Foundation v14 | `blockMesh snappyHexMesh` **`snappyHexMeshConfig`** `surfaceFeatures extrudeMesh extrude2DMesh refineMesh topoSet createBaffles polyDualMesh checkMesh`. No foamyHexMesh and no cfMesh |
| gmsh | `gmsh` 4.8.0 Python module inside WSL. The AetherCFD venv has pyvista 0.49 / VTK 9.7.1 / numpy but **no gmsh, trimesh or scipy** |

**cfMesh answer.** The v2306 source tree lists cfMesh as the `modules/cfmesh` git submodule [O2]. Our binary package ships `cartesianMesh` (its help text is tagged "(cfmesh)") but no `modules/` folder, no cfMesh tutorials and no user guide [LOCAL]. One guide claims ESI stopped bundling cfMesh around v2212 [O8], and our v2306 package contradicts that. So the dependency must be treated as fork- and version-specific, with capability detection at runtime.

Foundation v14's `snappyHexMeshConfig` writes `blockMeshDict`, `snappyHexMeshDict`, `surfaceFeaturesDict` and `meshQualityDict` from a set of surfaces. Its options include `-refinementLevel` (default 2), `-nCellsBetweenLevels` (3), `-layers`, `-firstLayerThickness`, `-layerExpansionRatio` (1.2), `-refinementDists`, `-cylindricalBackground` and `-baffles` [LOCAL][O6]. It treats a closed surface that encloses the others as an internal-flow boundary [O6]. Its output is a sound v14 baseline for our compiler to patch.

### 3.2 Size field → snappyHexMesh

1. **Background mesh.** Use cubic blockMesh cells of size Δ₀. Cubes aligned with flat surfaces snap best [O9]. Choose Δ₀ ≈ h_max, the far-field size.
2. **Level map.** Use `L(x) = clamp(⌈log₂(Δ₀/h(x))⌉, 0, L_max)`, because each refinement halves the edge length (§2.5).
3. **Surfaces.** For each patch, set `level (L_min L_max)` with L_min = L at the patch's median h and L_max = L at its minimum h. On v2306, express curvature with `curvatureLevel (n 0 L_κ −1)`, where n cells per radius ≈ N/(2π) [O3]. Express proximity with `gapLevel (n_g L_min L_max)`, with n_g > 3 recommended [O4]. Set the feature edge mesh level to L at the features. A string scan of v14's `libsnappyHexMesh.so` finds `gapLevel` and `insideSpan` but not `curvatureLevel`, `blockLevel`, `leakLevel`, `levelIncrement` or `nOuterIter`, which v2306 has [LOCAL]. The scan is a heuristic, so verify each keyword with a dry run. On v14, emit explicit distance regions in place of curvature levels.
4. **Volume regions from the Lipschitz field.** With `h(d) = h_w + g·d`, level L becomes admissible at `d_L = max(0, (Δ₀/2ᴸ − h_w)/g)`. Emit `mode distance; levels ((d_L L) …)`, increasing in distance [O5][O4]. For an arbitrary h(x) (wakes, flame zones), contour the voxelised h at the values Δ₀/2ᴸ (pyvista/VTK marching cubes), write each iso-surface as an STL, and use `mode inside; levels ((1e15 L))`.
5. **Gradation.** Octree buffers give an effective per-cell growth of about 2^(1/n). So set `nCellsBetweenLevels = ⌈1/log₂β⌉`. β = 1.2 gives 4, and β = 1.26 gives 3, which is the default [O5].

### 3.3 Feature and layer parameters

- **Feature angle θ_f.** Take the dihedral-angle histogram of the surface. Noise sits near 0–10°, so pick θ_f in the first gap above it, clamped to 30–60°.
- **Feature extraction.** `surfaceFeatureExtract` (ESI) or `surfaceFeatures` (v14) with `includedAngle = 180 − θ_f` [O12].
- **Feature resolution.** `resolveFeatureAngle = θ_f`. The documented default is 30, and tutorials use 30–60 [O5][O4]. Turn on explicit feature snapping and check the eMesh before meshing [O9].
- **Layers (ESI, following the tutorial comments).**
  - Set `featureAngle` to 60. Use 180 to extrude across every edge.
  - Set `slipFeatureAngle` and `layerTerminationAngle` to 0.5·featureAngle, and `mergePatchFacesAngle` to featureAngle.
  - Use `maxThicknessToMedialRatio 0.3`, `minMedialAxisAngle 90`, `nLayerIter 50`, and `nOuterIter > 1` to add layers in passes [O4].
  - Specify exactly two of: expansion ratio, first thickness, final thickness, total thickness [O5].
- **y+ recipe.** Compute Δy₁ from §2.6. Then, per patch with surface cell size h_s = Δ₀/2^{L_patch}:
  - write `relativeSizes true; firstLayerThickness Δy₁/h_s; expansionRatio r`;
  - choose `N = 1 + ⌈ln(λh_s/Δy₁)/ln r⌉` with λ ≈ 0.4, so the last layer blends into the core;
  - check `T ≤ 0.3·g_local`, and if the check fails, lower N or refine.

### 3.4 Extruded and axisymmetric detection

- **Extruded test.** Group coplanar faces (`trimesh.facets`) [Y2]. For each large facet normal d, test four conditions: (a) every other face has |n·d| < ε; (b) exactly two caps exist, with n = ±d; (c) `mesh.section` at 3–5 stations along d matches the cap outline (Hausdorff distance); (d) **V ≈ A_cap·L** [Y2]. Then mesh the profile in gmsh (quads plus a BL) and run `extrudeMesh` with `linearDirection` (or 1 layer and `empty` for 2D) [O13].
- **Axisymmetric test.** Use `trimesh.symmetry == "radial"` [Y2], check sections through the axis at several angles, and check **Pappus: V ≈ 2π·r̄·A_profile**. Mesh the half profile and run `extrudeMesh wedge` (`sectorCoeffs axis, angle`) [O13].

### 3.5 Pipes and annuli: blockMesh O-grid versus snappyHexMesh

Choose **blockMesh** when the recogniser finds circular or annular cross-sections along a straight, bent or tapered spine, or a sweepable volume, and when the physics rewards aligned hex cells (jets, pipe flows, LES). Generate it with **classy_blocks** (MIT). It provides Cylinder, Elbow, Frustum, Ring, ExtrudedRing and Shell shapes, a SimpleGrader or InflationGrader with first-cell thickness, and a vertex Optimizer that targets OpenFOAM quality [O11]. Otherwise use snappyHexMesh or cfMesh. Watch the core-to-O-block non-orthogonality jump [O10].

### 3.6 Thin gaps and walls

- **Measure.** Sample `trimesh.proximity.thickness`, interior for walls and `exterior=True` for fluid gaps [Y1]. A voxel alternative is `scipy.ndimage.distance_transform_edt` with `sampling = h` [L20]: take the ridge of the distance field, so g = 2D, and look up the nearest ridge through a KD-tree [L1][L4].
- **Gaps that can be resolved.** Use `gapLevel` [O4].
- **Gaps that cannot.** Close them with `blockLevel` [O4], or treat thin solids as baffles (`snappyHexMeshConfig -baffles`, `createBaffles`) [LOCAL].
- **Leaks.** Always set `locationsOutsideMesh` so leaks are reported [O4].

### 3.7 Polyhedral and tet routes

- **Ennova-style polyhedra.** Run gmsh tets (+ `extrudeBoundaryLayer` prisms along normals [G1]) → `gmshToFoam` → `polyDualMesh <featureAngle>`, which "creates the dual of a polyMesh, adhering to all the feature and patch edges" [LOCAL].
- **cfMesh `cartesianMesh`.** Gives mostly hexahedral cells with polyhedra at transitions [O14]. Its main parameters are `maxCellSize`, `minCellSize`, `boundaryCellSize`, `localRefinement`, `objectRefinements` and `boundaryLayers{nLayers; patchBoundaryLayers{thicknessRatio; maxFirstLayerThickness}}` [O15][O16].
- **Clean-up.** Finish with `improveMeshQuality` [LOCAL].

### 3.8 Wiring the loop

- **Generate.** Run the dictionaries compiled in §3.2–3.3, keeping every attempt in its own folder.
- **Check.** Run `checkMesh -allGeometry -allTopology -writeSets vtk` together with `-writeFields '(…)'`, which writes per-cell quality fields (check the field names with `-help-full`) [LOCAL]. Add `aethercfd.mesh.quality`, which already clusters bad cells into hotspots [LOCAL].
- **Diagnose.** Classify each hotspot by what it sits near: feature edge, layer front, gap or region transition.
- **Adjust.** Apply one rule per class, for example: raise the feature level or fix θ_f; reduce N or r; add `gapLevel`; increase `nCellsBetweenLevels`.

Bound the loop to K iterations and keep every attempt.

---

## 4. Ranked recommendations for AetherMesh

Ranked by value to a CFD researcher relative to implementation effort.

**1. Geometry intake and feature report** (high value, low effort). *Inputs:* STL, OBJ or STEP (STEP through gmsh/OCC). *Outputs:* watertightness, winding, non-manifold edges, open edges, the dihedral histogram with a suggested θ_f, feature curves written as an eMesh/VTK, and a bounding-box scale. *Algorithm:* trimesh adjacency angles and facets [Y2]; pyvista feature edges [Y3]; chaining and pruning (§2.2). *Failure handling:* when the surface is not watertight, offer a gmsh `classifySurfaces` reparametrisation [G1] or route to the wrap (#10). Never silently "fix" coordinates. This follows Ennova's principle of leaving xyz unchanged [E7].

**2. Size-field engine** (very high value, medium effort). *Inputs:* the surface, θ_f, N per circle, n_gap, β, h_min/h_max, and user boxes or wakes. *Outputs:* an h(x) octree or voxel grid with a provenance tag per cell (curvature, gap, feature or user). *Algorithm:* principal curvatures, proximity from max-sphere or EDT ridges, feature sizes, then the min of all components and a Lipschitz envelope by Dijkstra/fast marching [L1][L2][G3]. *Failure handling:* floor the local feature size at h_min near sharp edges [L5]. Report a predicted cell count before meshing, and refuse runs that exceed the budget.

**3. Size-field → mesher compiler** (very high value, low–medium effort). *Inputs:* h(x), the patch list and the fork profile. *Outputs:* `blockMeshDict`, `snappyHexMeshDict`, feature dictionaries and `meshQualityDict` for v2306 or v14. *Algorithm:* the §3.2–3.3 mappings (levels, curvature, gap, iso-surface regions, nCellsBetweenLevels from β). On v14, start from `snappyHexMeshConfig` output and patch it [O6]. *Failure handling:* probe each fork for keyword support. v14 appears to lack `curvatureLevel`, `blockLevel` and the leak keywords [LOCAL]. Degrade to distance regions with a warning.

**4. y+-driven layer planner** (high value, low effort). *Inputs:* U, ρ, μ, reference length, target y⁺ and wall patches. *Outputs:* Δy₁, N, r, T, and per-patch relative snappy entries or cfMesh `maxFirstLayerThickness`. *Algorithm:* the §2.6 correlation chain [L14] with relative-size conversion (§3.3). *Failure handling:* if T exceeds 0.3 × the local gap, cut N or r. After meshing, parse layer coverage (the snappy log or the `nSurfaceLayers` field [O9]). If coverage stays below a threshold, add layers in `nOuterIter` passes (v2306) or fall back (#9).

**5. Check–diagnose–adjust loop** (high value, medium effort). *Inputs:* the mesh and quality targets. *Outputs:* an accepted mesh plus an audit trail. *Algorithm:* §3.8. It optionally includes a "sweep mode" modelled on snappyLayerTests: vary parameters, score quality, coverage and a short solver stability run, and pick the trade-off [O9]. *Failure handling:* bounded iterations, keep the best-so-far mesh, and never overwrite the user's `polyMesh` without confirmation.

**6. Extruded and axisymmetric recogniser → 2D or wedge pipeline** (high value, low–medium effort). *Inputs:* the surface. *Outputs:* the recognised class, the axis or direction, and the profile. Then a gmsh 2D mesh with BL → `extrudeMesh` (`linearDirection` or `wedge`) [O13]. *Algorithm:* §3.4 tests, including V = A·L and Pappus. *Failure handling:* accept a class only if all tests pass within tolerance. Otherwise show it as a suggestion, never as an automatic switch.

**7. O-grid templates for pipes, ducts and annuli** (medium–high value, medium effort). *Inputs:* the recognised spine and radii, or user parameters. *Outputs:* a blockMeshDict built through classy_blocks shapes, with inflation grading to the planner's Δy₁ [O11]. *Algorithm:* fit circles to `mesh.section` slices [Y2], fit the spine, and pick the shape. Core size is a tunable fraction of R, optimised against non-orthogonality [O10]. *Failure handling:* if a fit's residual is high or a junction topology is unsupported, fall back to snappyHexMesh.

**8. Thin-gap, thin-wall and leak handling** (medium value, low–medium effort). *Inputs:* the surface and h(x). *Outputs:* gap and wall maps, plus `gapLevel`/`blockLevel`, baffle lists and `locationsOutsideMesh` entries. *Algorithm:* §3.6. *Failure handling:* if a leak is detected, show the leak path (as ESI does [O4] and Ennova does [E21]) and stop. If a gap cannot be resolved within the cell budget, ask the user to choose between closing it and baffling it.

**9. Mesher portfolio with automatic fallback** (medium value, medium effort). *Inputs:* the job plus capability detection per fork [LOCAL]. *Outputs:* the best of snappyHexMesh, cfMesh `cartesianMesh`/`pMesh`, and gmsh tet+prism → `polyDualMesh`. *Algorithm:* choose by geometry class and by the failure history from #5. *Failure handling:* each backend has its own validator. cfMesh exists only on v2306 here, and its availability in other ESI builds is uncertain [O2][O8].

**10. Shrink-wrap for dirty CAD** (medium value, medium–high effort). *Inputs:* a non-watertight assembly, h_wrap, a gap-close tolerance, and a seed point or outside point. *Outputs:* a watertight STL. *Algorithm:* voxel or octree flood fill from the seed, the EDT-based offset surface, marching cubes, projection to the nearest point with angle-based reprojection, then an isotropic remesh. This is the pattern public in [P1] and in Ennova's wrap-then-remesh workflow [E19][E20]. *Failure handling:* flag features lost to the wrap by comparing Hausdorff distance against θ_f-features. Keep internal baffles optional [E17].

**Deferred:** medial-axis multiblock decomposition [L6][L7], 3D frame-field or polycube hex [L8][L10], and ML-based sizing. Ennova itself sells no AI meshing [E1]. 2D cross-field quads through gmsh [G2] could go into #6 cheaply once gmsh is upgraded.

---

## References

**Ennova (vendor material, accessed 2026-10-07)**
- [E1] Home — https://ennova-cfd.com/
- [E2] About — https://ennova-cfd.com/about.html
- [E3] Products — https://ennova-cfd.com/products.html
- [E4] Ennova Blue — https://ennova-cfd.com/ennova-blue.html
- [E5] EnnovaCFD for OpenFOAM — https://ennova-cfd.com/openfoam.html
- [E6] Mesh generation — https://ennova-cfd.com/mesh-generation.html
- [E7] CAD repair — https://ennova-cfd.com/cadrepair.html
- [E8] Vision (Hohmeyer) — https://ennova-cfd.com/vision.html
- [E9] What Ennova can do — https://ennova-cfd.com/services-detail.html
- [E10] Purchase / price list — https://ennova-cfd.com/purchase.html
- [E11] Licensing — https://ennova-cfd.com/license.html
- [E12] Downloads — https://ennova-cfd.com/downloads.html
- [E13] Release notes 1.11.2 — https://ennova-cfd.com/Ennova_CFD_1.11.2_Release_Notes.pdf
- [E14] "Comparing Polyhedral and Hex-Core Meshes Using OpenFOAM" (AutoCFD4, Sept 2024) — https://ennova-cfd.com/tutorial-pdfs/Ennova-AutoCFD4-v02.pdf
- [E15] User guide: Introduction — https://ennova-cfd.com/dr-explain/introduction.htm
- [E16] User guide: Architecture — https://ennova-cfd.com/dr-explain/ennova_architecture.htm
- [E17] User guide: Mesh setup parameters — https://ennova-cfd.com/dr-explain/meshsetup_Button.htm
- [E18] User guide: Smooth (Booster) — https://ennova-cfd.com/dr-explain/smoothsufMesh_Button.htm
- [E19] User guide: Shrinkwrap — https://ennova-cfd.com/dr-explain/shrinkWrap_Button.htm
- [E20] User guide: Remesh — https://ennova-cfd.com/dr-explain/2.htm
- [E21] User guide: Leak detection — https://ennova-cfd.com/dr-explain/qsetupLeakDetect_Button.htm
- [E22] User guide: Density regions — https://ennova-cfd.com/dr-explain/createDensity_Button.htm
- [E23] Blog: AIAA Aviation 2018 (H+) — https://ennova-cfd.com/blog-detail5.html
- [E24] Export formats — https://ennova-cfd.com/export.html
- [E25] CAD import — https://ennova-cfd.com/import.html
- [E26] Ennova Marine — https://ennova-cfd.com/ennova-marine.html
- [E27] OEM toolkit — https://ennova-cfd.com/oem.html
- [E28] User guide: Mesh settings (Thin Cut, Leak tool) — https://ennova-cfd.com/dr-explain/mesh_settings.htm
- [E29] Services — https://ennova-cfd.com/services.html
- [P1] US 9,922,453 B1, Shrink wrap generation (MSC Software / Hexagon) — https://patents.google.com/patent/US9922453
- [P2] US 11,282,274, Conformal connections between meshes (Ansys) — https://patents.google.com/patent/US11282274

**OpenFOAM and meshers**
- [O1] OpenFOAM v2306 release — https://www.openfoam.com/news/main-news/openfoam-v2306
- [O2] v2306 `.gitmodules` (cfmesh submodule) — https://gitlab.com/openfoam/core/openfoam/-/raw/OpenFOAM-v2306/.gitmodules
- [O3] v2306 tutorial `block_with_curvature` (curvatureLevel) — https://gitlab.com/openfoam/core/openfoam/-/raw/OpenFOAM-v2306/tutorials/mesh/snappyHexMesh/block_with_curvature/system/snappyHexMeshDict
- [O4] v2306 snappyHexMesh tutorials (gap_detection, opposite_walls, sphere_gapClosure, airfoilWithLayers, addLayersToFaceZone, aerofoilNACA0012_directionalRefinement, motorBike_leakDetection; DTCHullMoving for layer angles) — e.g. https://gitlab.com/openfoam/core/openfoam/-/raw/OpenFOAM-v2306/tutorials/mesh/snappyHexMesh/gap_detection/system/snappyHexMeshDict (same tree for the others)
- [O5] OpenFOAM v13 user guide: snappyHexMesh — https://doc.cfd.direct/openfoam/user-guide-v13/snappyhexmesh
- [O6] snappyHexMeshConfig source/description — https://cpp.openfoam.org/v13/snappyHexMeshConfig_8C_source.html
- [O7] OpenFOAM 2.3.0: foamyHexMesh — https://openfoam.org/release/2-3-0/foamyhexmesh
- [O8] "Complex geometry meshing with OpenFOAM on WSL" (cfMesh bundling) — https://thedigitalengineer.substack.com/p/complex-geometry-meshing-with-openfoam
- [O9] T. Keskitalo, "SnappyHexMesh in Practice", Finnish OpenFOAM User Day 2025 — https://tkeskita.kapsi.fi/OF/Keskitalo_FOFUD_2025.pdf
- [O10] CFD Direct notes: block-structured meshes (O-grid pipe) — https://doc.cfd.direct/notes/cfd-general-principles/block-structured-meshes
- [O11] classy_blocks — https://github.com/damogranlabs/classy_blocks
- [O12] v2306 annotated surfaceFeatureExtractDict — https://gitlab.com/openfoam/core/openfoam/-/raw/OpenFOAM-v2306/etc/caseDicts/annotated/surfaceFeatureExtractDict
- [O13] v2306 annotated extrudeMeshDict — https://gitlab.com/openfoam/core/openfoam/-/raw/OpenFOAM-v2306/etc/caseDicts/annotated/extrudeMeshDict
- [O14] cfMesh User Guide v1.0 — https://code.hlrs.de/exaFOAM/foam-extend4.1-coherent-io/raw/commit/1255b6a88626982f1590b4d8d390f10098135e3c/doc/cfMesh/User%20Guide%20-%20cfMesh%20v1.0.pdf
- [O15] cfMesh generateBoundaryLayers source — https://develop.openfoam.com/Community/integration-cfmesh/-/blame/20ca801a3ac0b35064ad92a3aa5482c24e70c3f4/utilities/generateBoundaryLayers/generateBoundaryLayers.C
- [O16] cfMesh tutorial meshDict changes — https://code.hlrs.de/exaFOAM/foam-extend4.1-coherent-io/commit/438dd0a3ab737655cf9a005117616e1ed50db740

**Gmsh and Python libraries**
- [G1] Gmsh reference manual (t10 fields, t13 classifySurfaces, MeshSizeFromCurvature, extrudeBoundaryLayer, optimize) — https://gmsh.info/doc/texinfo/gmsh.html
- [G2] Reberol, Georgiadis, Remacle, quasi-structured quad meshing in Gmsh (2021) — https://arxiv.org/abs/2103.04652
- [G3] Bawin, Henrotte, Remacle, automatic feature-preserving size field (2020) — https://arxiv.org/abs/2009.03984
- [Y1] trimesh `proximity.py` (thickness, max_tangent_sphere) — https://raw.githubusercontent.com/mikedh/trimesh/main/trimesh/proximity.py
- [Y2] trimesh `base.py` (face_adjacency_angles, facets, symmetry, section) — https://raw.githubusercontent.com/mikedh/trimesh/main/trimesh/base.py
- [Y3] pyvista `extract_feature_edges` — https://docs.pyvista.org/api/core/_autosummary/pyvista.DataSetFilters.extract_feature_edges.html

**Literature**
- [L1] P.-O. Persson, Mesh size functions for implicit geometries and PDE-based gradient limiting, Eng. Comput. 22 (2006) — https://doi.org/10.1007/s00366-006-0014-1
- [L2] Borouchaki, Hecht, Frey, Mesh gradation control, IJNME 43 (1998), as cited in — https://webapp.tudelft.nl/proceedings/ect2012/html/summary/laug.htm
- [L3] Local feature size (definitions, Amenta–Bern / Ruppert) — https://en.wikipedia.org/wiki/Local_feature_size
- [L4] Quadros, Shimada, Owen, skeleton-based sizing (SM 2004) — https://diglib7.eg.org/handle/10.2312/sm20041410 ; Cubit skeleton sizing — https://sandia.gov/files/cubit/15.8/help_manual/WebHelp/mesh_generation/adaptivity_and_sizing_functions/geom_adaptive_sizing_function.htm
- [L5] Boissonnat & Oudot, Provably good sampling and meshing of Lipschitz surfaces — https://researchportal.ip-paris.fr/en/publications/provably-good-sampling-and-meshing-of-lipschitz-surfaces/
- [L6] QUB medial-axis/medial-surface subdivision (Tam & Armstrong; Price, Armstrong, Sabin) — https://www.qub.ac.uk/research-centres/FiniteElementModellingGroup/ResearchTopics/Meshgeneration/Automaticmeshgeneration ; https://pure.qub.ac.uk/en/publications/hexahedral-mesh-generation-by-medial-surface-subdivision-part-ii-/
- [L7] Critique of medial-surface templates — https://people.eecs.berkeley.edu/%7Ejrs/meshpapers/SERB.pdf ; R. Schneiders, quad/hex algorithms notes — https://www.robertschneiders.de/papers/vki.pdf
- [L8] Pietroni et al., Hex-mesh generation and processing: a survey, ACM TOG (2022) — https://arxiv.org/abs/2202.12670
- [L9] Cubit sweep and automatic scheme selection — https://sandia.gov/files/cubit/15.9/help_manual/WebHelp/mesh_generation/meshing_schemes/traditional/sweep.htm ; https://sandia.gov/files/cubit/15.9/help_manual/WebHelp/step_by_step_tutorials/command_line/step_08.htm
- [L10] Gregson, Sheffer, Zhang, All-hex meshing via volumetric PolyCube deformation (SGP 2011) — https://www.cs.ubc.ca/labs/imager/tr/2011/HexMeshingPolycubeDeformation ; PolyCut — https://www.cs.ubc.ca/~sheffa/hexing/hexing.htm
- [L11] Garimella & Shephard, Boundary layer mesh generation for viscous flow simulations, IJNME 49 — https://scorec.rpi.edu/reports/view_report.php?id=58
- [L12] Aubry & Löhner, ridges/corners and point normals (draft) — https://www.scipedia.com/public/Aubry_Lohner_2007a ; publication list incl. "most normal" normal and concave-corner entropy papers — https://dblp.org/pid/49/3795
- [L13] Loseille & Löhner, Robust boundary layer mesh generation, IMR 2012 — https://dblp.dagstuhl.de/db/conf/imr/imr2012.html
- [L14] Skin-friction correlations — https://www.cfd-online.com/Wiki/Skin_friction_coefficient ; y+ calculator using Schlichting — https://www.mathworks.com/matlabcentral/fileexchange/75199-y-plus-calculator ; cell-height note — https://simscale.com/forum/t/wall-distance-experience-do-these-calculations-look-right-y-yplus/86796
- [L15] Laplacian invalidity and smart/optimisation smoothing — https://library.eecs.utk.edu/files/ut-cs-97-363.pdf ; https://arxiv.org/pdf/cs/9809081
- [L16] Escobar et al., Simultaneous untangling and smoothing of tetrahedral meshes, CMAME 192 (2003) — https://www.dca.iusiani.ulpgc.es/proyecto0204/pdf/sustm.pdf ; code — https://www.dca.iusiani.ulpgc.es/SUScode
- [L17] Mesquite mesh quality improvement toolkit — https://www.osti.gov/biblio/1230520
- [L18] Knupp, target-matrix paradigm — https://www.sandia.gov/research/publications/details/introducing-the-target-matrix-paradigm-for-mesh-optimization-via-node-movem-2012-01-01/ ; TMOP for high-order meshes — https://arxiv.org/abs/1807.09807
- [L19] Slotnick et al., CFD Vision 2030 Study — https://ntrs.nasa.gov/citations/20140003093
- [L20] scipy `distance_transform_edt` — https://docs.scipy.org/doc/scipy/reference/generated/scipy.ndimage.distance_transform_edt.html
