"""Turbulent combustion model advisor: which model, chemistry solver and turbulence tweaks fit the flame,
the question being asked and the budget -- with the reasons, the caveats, and the exact OpenFOAM dictionary
entries for the fork in use (ESI v2306: constant/combustionProperties; Foundation v14:
constant/reactionProperties; both verified against the installed tutorials). No Qt here."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Situation:
    flame: str = "non-premixed"  # premixed | non-premixed | partially-premixed
    regime: str | None = None  # from calc.regime(): corrugated flamelets | thin reaction zones | ...
    Da: float | None = None
    Ka: float | None = None
    want: tuple[str, ...] = ("temperature",)  # temperature | CO | NOx | soot | extinction | ignition
    species: int = 53  # mechanism size
    cells: int = 100_000
    steady: bool = True
    round_jet: bool = False
    fork: str = "esi"  # esi | foundation


@dataclass
class Advice:
    model: str
    why: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)  # relative path -> dictionary text (entries)
    turbulence: list[str] = field(default_factory=list)
    chemistry: list[str] = field(default_factory=list)
    score: float = 0.0


def _combustion_dict(fork: str, model: str, coeffs: str) -> tuple[str, str]:
    if fork == "foundation":
        return "constant/reactionProperties", f"reactionModel  {model};\n\n{model}\n{{\n{coeffs}}}\n"
    return "constant/combustionProperties", (f"combustionModel  {model};\n\nactive  true;\n\n{model}Coeffs\n{{\n"
                                             f"{coeffs}}}\n")


def _chemistry(fork: str, species: int, steady: bool) -> tuple[str, str, list[str]]:
    notes = []
    stiff = "seulex" if species > 30 else "Rosenbrock34"
    tdac = ""
    if species > 80:
        notes.append(f"{species} species: enable TDAC (ISAT tabulation + DAC reduction) or use a reduced mechanism; "
                     "cost scales roughly with species^2-3 per cell")
        tdac = ("\ntabulation\n{\n    method ISAT;\n    tolerance 1e-4;\n}\n\nreduction\n{\n    method DAC;\n"
                "    tolerance 1e-4;\n}\n")
    if fork == "foundation":
        txt = (f"initialChemicalTimeStep 1e-7;\n\node\n{{\n    solver          {stiff};\n    absTol          1e-8;\n"
               f"    relTol          1e-1;\n}}\n{tdac}")
    else:
        txt = (f"chemistryType\n{{\n    solver          ode;\n}}\n\nchemistry           on;\n\ninitialChemicalTimeStep "
               f"1e-7;\n\nodeCoeffs\n{{\n    solver          {stiff};\n    absTol          1e-8;\n    relTol          "
               f"1e-1;\n}}\n{tdac}")
    notes.append(f"stiff ODE solver {stiff} (implicit, extrapolation/Rosenbrock): chemistry time scales are far "
                 "below the flow time step")
    return "constant/chemistryProperties", txt, notes


def advise(s: Situation) -> list[Advice]:
    out: list[Advice] = []
    want = set(s.want)
    finite_rate = bool(want & {"CO", "NOx", "extinction", "ignition", "soot"})
    # --- EDC
    a = Advice("EDC (Eddy Dissipation Concept, Magnussen; v2005 constants)")
    a.score = 3.0 + (2 if s.flame != "premixed" else 0) + (1.5 if finite_rate else 0)
    a.why += ["finite-rate chemistry in fine structures: CO and NO trends come from the mechanism",
              "robust and widely validated for piloted jet flames (Sandia D/E/F) in RANS"]
    a.caveats += ["over-predicts the temperature near the nozzle in strongly sheared jets unless the turbulence is "
                  "right; the round-jet anomaly needs the turbulence fix below",
                  "steady EDC needs an LTS or pseudo-transient approach for robustness"]
    path, txt = _combustion_dict(s.fork, "EDC", "    version v2005;\n")
    a.files[path] = txt
    out.append(a)
    # --- PaSR
    a = Advice("PaSR (Partially Stirred Reactor, Chomiak & Karlsson)")
    a.score = 2.8 + (1 if s.Da is not None and s.Da < 10 else 0) + (1 if finite_rate else 0)
    a.why += ["reacting fraction kappa = tau_c/(tau_c + tau_mix) blends finite-rate chemistry with turbulent "
              "mixing: good where Da ~ 1 (slow chemistry, local extinction tendencies)"]
    a.caveats += ["result depends on Cmix (mixing time constant, typical 0.1-1): calibrate once against data"]
    path, txt = _combustion_dict(s.fork, "PaSR", "    Cmix 0.5;\n")
    a.files[path] = txt
    out.append(a)
    # --- laminar
    a = Advice("laminar (no turbulence-chemistry interaction)")
    a.score = 1.0 + (4 if s.regime in ("laminar",) or (s.Ka is not None and s.Ka < 0.1 and s.Da and s.Da > 100) else 0)
    a.why += ["exact for laminar flames or DNS-grade meshes (>= 10 cells per flame thickness)"]
    a.caveats += ["in a RANS run on a coarse mesh this ignores turbulent fluctuations entirely: wrong temperatures"]
    path, txt = _combustion_dict(s.fork, "laminar", "")
    a.files[path] = txt
    out.append(a)
    # --- infinitely fast / diffusion
    if not finite_rate:
        a = Advice("infinitelyFastChemistry / diffusion (fast, mixing-limited)")
        a.score = 2.5 + (1.5 if s.cells > 2_000_000 else 0)
        a.why += ["cheapest: one global step, temperature field only; fine for heat-release studies"]
        a.caveats += ["no CO/NO/soot information, no extinction"]
        path, txt = _combustion_dict(s.fork, "infinitelyFastChemistry", "    semiImplicit no;\n    C 5.0;\n")
        a.files[path] = txt
        out.append(a)
    # --- flamelets (external)
    a = Advice("Steady laminar flamelets / FPV (tabulated chemistry)")
    a.score = 2.6 + (1.5 if s.species > 80 else 0) + (1 if s.flame != "premixed" else 0)
    a.why += ["chemistry pre-tabulated in mixture fraction and scalar dissipation (or progress variable): cost "
              "independent of mechanism size, so detailed CRECK mechanisms become affordable",
              "AetherCFD's Combustion panel can generate counterflow flamelets with Cantera"]
    a.caveats += ["not in stock OpenFOAM: needs an external solver/library (e.g. the CRECK flamelet model in your "
                  "RPL project) and a presumed-PDF integration step",
                  "slow processes (NO formation, soot) need extra transported variables"]
    out.append(a)
    # turbulence + chemistry for all
    for adv in out:
        if s.round_jet:
            adv.turbulence.append("round jet: standard k-epsilon over-predicts spreading (round-jet anomaly). Use "
                                  "C1 = 1.6 (kEpsilonCoeffs { C1 1.6; }) or Pope's vortex-stretching correction "
                                  "(a custom turbulence model -- see Code > New model from template)")
        if "soot" in want:
            adv.caveats.append("soot: stock OpenFOAM only has the simple mixtureFractionSoot radiation model; "
                               "detailed soot (method of moments, e.g. sootlib/MoM) needs a coupled custom fvModel")
        if adv.model.startswith(("EDC", "PaSR", "laminar")):
            path, txt, notes = _chemistry(s.fork, s.species, s.steady)
            adv.files.setdefault(path, txt)
            adv.chemistry += notes
    out.sort(key=lambda x: -x.score)
    return out
