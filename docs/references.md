# Methods and references

AetherCFD Studio implements methods that other people developed, and it is built on other people's software.
This page lists them with the place in the code where each method is used. If you publish results made with
AetherCFD, please cite OpenFOAM and the methods you used, as well as AetherCFD itself ([CITATION.cff](../CITATION.cff)).

## Software it is built on

- **OpenFOAM**, which does every simulation: H. G. Weller, G. Tabor, H. Jasak and C. Fureby, "A tensorial
  approach to computational continuum mechanics using object-oriented techniques", *Computers in Physics*
  12(6), 620–631, 1998. doi:10.1063/1.168744. Distributions: ESI-OpenCFD (www.openfoam.com) and The OpenFOAM
  Foundation (openfoam.org).
- **VTK**: W. Schroeder, K. Martin and B. Lorensen, *The Visualization Toolkit*, 4th ed., Kitware, 2006.
- **PyVista**: C. B. Sullivan and A. Kaszynski, "PyVista: 3D plotting and mesh analysis through a streamlined
  interface for the Visualization Toolkit (VTK)", *Journal of Open Source Software* 4(37), 1450, 2019.
  doi:10.21105/joss.01450.
- **Cantera**: D. G. Goodwin, H. K. Moffat, I. Schoegl, R. L. Speth and B. W. Weber, *Cantera: An
  object-oriented software toolkit for chemical kinetics, thermodynamics, and transport processes*,
  https://www.cantera.org. Use the version-specific citation that Cantera gives for the version you run.
- **NumPy**: C. R. Harris et al., "Array programming with NumPy", *Nature* 585, 357–362, 2020.
  doi:10.1038/s41586-020-2649-2.
- **SciPy**: P. Virtanen et al., "SciPy 1.0: fundamental algorithms for scientific computing in Python",
  *Nature Methods* 17, 261–272, 2020. doi:10.1038/s41592-019-0686-2.
- **Matplotlib**: J. D. Hunter, "Matplotlib: A 2D graphics environment", *Computing in Science & Engineering*
  9(3), 90–95, 2007. doi:10.1109/MCSE.2007.55.
- **PyTorch** (optional, for PINNs): A. Paszke et al., "PyTorch: An imperative style, high-performance deep
  learning library", *Advances in Neural Information Processing Systems* 32, 2019.
- **Qt and Qt for Python (PySide6)**, the **Qt Advanced Docking System**, **pyqtgraph**, **QtAwesome**,
  **xterm.js** and the other components in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

## Verification

- **Grid convergence index** (`case/gridstudy.py`): I. B. Celik, U. Ghia, P. J. Roache, C. J. Freitas, H. Coleman
  and P. E. Raad, "Procedure for estimation and reporting of uncertainty due to discretization in CFD
  applications", *Journal of Fluids Engineering* 130(7), 078001, 2008. doi:10.1115/1.2960953.
- **Safety factor Fs = 1.25 for three-grid studies** (`case/gridstudy.py`): P. J. Roache, "Perspective: A method
  for uniform reporting of grid refinement studies", *Journal of Fluids Engineering* 116(3), 405–413, 1994.
  doi:10.1115/1.2910291.

## Meshing and near-wall resolution

- **Flat-plate skin friction** Cf = (2 log10 Re_x − 0.65)^−2.3 (`mesh/wall.py`): H. Schlichting,
  *Boundary-Layer Theory*, 7th ed., McGraw-Hill, 1979.
- **Blasius friction factor** for pipes and ducts (`mesh/wall.py`): H. Blasius, "Das Ähnlichkeitsgesetz bei
  Reibungsvorgängen in Flüssigkeiten", *Forschungsheft des Vereins Deutscher Ingenieure* 131, 1913.
- **Sutherland's law** for the viscosity of air (`mesh/wall.py`, `case/gates.py`): W. Sutherland, "The viscosity
  of gases and molecular force", *Philosophical Magazine* 36(223), 507–531, 1893.

## Turbulence and combustion

- **Round-jet correction to k-epsilon** (`codegen/templates.py`): S. B. Pope, "An explanation of the turbulent
  round-jet/plane-jet anomaly", *AIAA Journal* 16(3), 279–281, 1978. doi:10.2514/3.7521.
- **Mixture fraction** from element mass fractions (`combustion/calc.py`): R. W. Bilger, S. H. Stårner and R. J.
  Kee, "On reduced mechanisms for methane-air combustion in nonpremixed flames", *Combustion and Flame* 80,
  135–149, 1990.
- **Premixed combustion regimes** (`combustion/calc.py`): N. Peters, *Turbulent Combustion*, Cambridge
  University Press, 2000; R. Borghi, "On the structure and morphology of turbulent premixed flames", in *Recent
  Advances in the Aerospace Sciences*, Springer, 117–138, 1985.
- **GRI-Mech 3.0** (the default methane mechanism, through Cantera): G. P. Smith, D. M. Golden, M. Frenklach et
  al., http://combustion.berkeley.edu/gri-mech/.

## Post-processing and analysis

- **Q-criterion** (`results/iso.py`): J. C. R. Hunt, A. A. Wray and P. Moin, "Eddies, streams, and convergence
  zones in turbulent flows", Center for Turbulence Research, Proceedings of the Summer Program 1988, 193–208.
- **Isosurfaces by marching tetrahedra** (`results/iso.py`): A. Doi and A. Koide, "An efficient method of
  triangulating equi-valued surfaces by using tetrahedral cells", *IEICE Transactions* E74(1), 214–224, 1991.
- **Numerical schlieren** exp(−k |∇ρ| / |∇ρ|ref) (`results/pipeline.py`): J. J. Quirk, "A contribution to the
  great Riemann solver debate", *International Journal for Numerical Methods in Fluids* 18(6), 555–574, 1994.
- **Proper orthogonal decomposition, method of snapshots** (`results/modes.py`): L. Sirovich, "Turbulence and
  the dynamics of coherent structures. Part I: Coherent structures", *Quarterly of Applied Mathematics* 45(3),
  561–571, 1987.
- **Dynamic mode decomposition** (`results/modes.py`): P. J. Schmid, "Dynamic mode decomposition of numerical
  and experimental data", *Journal of Fluid Mechanics* 656, 5–28, 2010. doi:10.1017/S0022112010001217.
  Exact DMD: J. H. Tu, C. W. Rowley, D. M. Luchtenburg, S. L. Brunton and J. N. Kutz, "On dynamic mode
  decomposition: Theory and applications", *Journal of Computational Dynamics* 1(2), 391–421, 2014.
  doi:10.3934/jcd.2014.1.391. Hankel (time-delay) DMD: H. Arbabi and I. Mezić, "Ergodic theory, dynamic mode
  decomposition, and computation of spectral properties of the Koopman operator", *SIAM Journal on Applied
  Dynamical Systems* 16(4), 2096–2126, 2017. doi:10.1137/17M1125236.
- **Gaussian-process surrogates** with an ARD squared-exponential kernel, fitted by the log marginal likelihood
  (`ml/surrogate.py`): C. E. Rasmussen and C. K. I. Williams, *Gaussian Processes for Machine Learning*, MIT
  Press, 2006.
- **Optimal interpolation** of measurements into a field (`assim/measurements.py`): E. Kalnay, *Atmospheric
  Modeling, Data Assimilation and Predictability*, Cambridge University Press, 2003.

## Physics-informed neural networks

- **PINNs** (`ml/pinn_worker.py`): M. Raissi, P. Perdikaris and G. E. Karniadakis, "Physics-informed neural
  networks: A deep learning framework for solving forward and inverse problems involving nonlinear partial
  differential equations", *Journal of Computational Physics* 378, 686–707, 2019. doi:10.1016/j.jcp.2018.10.045.
- **Fourier feature inputs** (`ml/pinn_worker.py`): M. Tancik et al., "Fourier features let networks learn high
  frequency functions in low dimensional domains", *Advances in Neural Information Processing Systems* 33, 2020.

## Further reading

[docs/vision/roadmap.md](vision/roadmap.md) and [docs/research/automeshing_survey.md](research/automeshing_survey.md)
cite the papers, vendor documents and OpenFOAM release notes that the roadmap is based on. Those are surveys of
what exists, not methods AetherCFD implements.
