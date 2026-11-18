# Test case fixtures

The cases in this folder are copies of OpenFOAM tutorial cases, used as test data. They are not covered by
AetherCFD's Apache-2.0 license. OpenFOAM, including its tutorials, is free software under the GNU General
Public License version 3 or later (GPL-3.0-or-later); the text is in [COPYING](COPYING).

| Folder | Source | Copyright |
|---|---|---|
| `esi_cavity` | OpenFOAM v2306 (ESI-OpenCFD), tutorial `incompressible/icoFoam/cavity/cavity` | OpenCFD Ltd |
| `esi_pitzDaily` | OpenFOAM v2306 (ESI-OpenCFD), tutorial `incompressible/simpleFoam/pitzDaily` | OpenCFD Ltd |
| `esi_multiRegionHeater` | OpenFOAM v2306 (ESI-OpenCFD), tutorial `heatTransfer/chtMultiRegionFoam/multiRegionHeater` | OpenCFD Ltd |
| `of14_cavity` | OpenFOAM 14 (The OpenFOAM Foundation), tutorial `incompressibleFluid/cavity` | The OpenFOAM Foundation |

Changes: some cases were run and keep one solution time or a solver log; files that the tests do not need were
left out. `esi_multiRegionHeater/README.md` lists what was kept.

OPENFOAM® is a registered trade mark of OpenCFD Limited, producer and distributor of the OpenFOAM software via
www.openfoam.com. AetherCFD is not approved or endorsed by OpenCFD Limited or by The OpenFOAM Foundation.
