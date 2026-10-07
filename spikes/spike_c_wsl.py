"""Spike C — WSL behaviour the run manager depends on. Only touches
D:\\OpenFOAM\\_aethercfd_scratch\\spikeC (= /mnt/d/OpenFOAM/_aethercfd_scratch/spikeC) and never calls
wsl --shutdown / --terminate.

  1. fork preludes (ESI v2306, Foundation v14) and the mixed-fork refusal (exit 97)
  2. argv containing spaces survives wsl.exe
  3. write-now (SIGUSR1) / stop-at-write-now (SIGUSR2) per fork
  4. a detached run survives the launching wsl.exe exiting (with and without an anchor client)
  5. decomposed case loads with vtkPOpenFOAMReader (CaseType=0)

Run: uv run python spikes/spike_c_wsl.py [--only 1,2,3,4,5]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

DISTRO = "LMAE-OpenFOAM"
WIN_ROOT = Path(r"D:\OpenFOAM\_aethercfd_scratch\spikeC")
WSL_ROOT = "/mnt/d/OpenFOAM/_aethercfd_scratch/spikeC"
FORKS = {
    "esi": {"bashrc": "/usr/lib/openfoam/openfoam2306/etc/bashrc", "dir": "/usr/lib/openfoam/openfoam2306"},
    "of14": {"bashrc": "/opt/openfoam14/etc/bashrc", "dir": "/opt/openfoam14"},
}
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008

ap = argparse.ArgumentParser()
ap.add_argument("--only", default="1,2,3,4,5")
args = ap.parse_args()
ONLY = {int(x) for x in args.only.split(",")}
report: dict = {"checks": {}}


def check(name, ok, detail=None):
    report["checks"][name] = {"ok": bool(ok), "detail": detail}
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail is not None else ""), flush=True)


def prelude(fork: str) -> str:
    f = FORKS[fork]
    return f"""export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
if [ -n "${{WM_PROJECT_DIR:-}}" ]; then echo "AETHER: refusing, OpenFOAM already sourced: $WM_PROJECT_DIR" >&2; exit 97; fi
set +u
. {f['bashrc']} >/dev/null 2>&1
if [ "${{WM_PROJECT_DIR:-}}" != "{f['dir']}" ]; then echo "AETHER: fork env mismatch: ${{WM_PROJECT_DIR:-unset}}" >&2; exit 98; fi
"""


def wsl(body: str, *argv: str, timeout: float = 600, name: str = "step") -> tuple[int, str]:
    """Write an LF-only script into scratch and run it with an argument list (never a shell string)."""
    sd = WIN_ROOT / "scripts"
    sd.mkdir(parents=True, exist_ok=True)
    p = sd / f"{name}.sh"
    p.write_bytes(("#!/bin/bash\n" + body).encode("utf-8"))  # bytes: guaranteed LF, no CRLF translation
    cmd = ["wsl.exe", "-d", DISTRO, "--cd", "/", "-e", "/bin/bash", "--noprofile", "--norc",
           f"{WSL_ROOT}/scripts/{name}.sh", *argv]
    env = dict(os.environ, WSL_UTF8="1")
    r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env, creationflags=CREATE_NO_WINDOW)
    out = (r.stdout + r.stderr).decode("utf-8", errors="replace")
    return r.returncode, out


def running_distros() -> list[str]:
    r = subprocess.run(["wsl.exe", "-l", "--running", "-q"], capture_output=True,
                       env=dict(os.environ, WSL_UTF8="1"), creationflags=CREATE_NO_WINDOW)
    return [ln.strip() for ln in r.stdout.decode("utf-8", errors="replace").splitlines() if ln.strip()]


# ------------------------------------------------------------------ 1. preludes
if 1 in ONLY:
    for fork in FORKS:
        rc, out = wsl(prelude(fork) + 'echo "OK $WM_PROJECT_VERSION"\n', name=f"prelude_{fork}")
        check(f"prelude {fork} sources exactly its fork", rc == 0 and "OK" in out, out.strip()[-80:])
    rc, out = wsl(prelude("esi") + prelude("of14") + 'echo "SHOULD NOT GET HERE"\n', name="prelude_mixed")
    check("mixed-fork script is refused with exit 97", rc == 97 and "SHOULD NOT" not in out, f"rc={rc} {out.strip()[-90:]}")

# ------------------------------------------------------------------ 2. argv with spaces
if 2 in ONLY:
    spacey = "/mnt/d/OpenFOAM/RPL Apollo Lunar Module Ascent Engine"
    rc, out = wsl('printf "[%s]\\n" "$1"; ls -d "$1" >/dev/null && echo EXISTS\n', spacey, "second arg", name="argv")
    check("argv with spaces survives wsl.exe", f"[{spacey}]" in out and "EXISTS" in out, out.strip())


# ------------------------------------------------------------------ 3. signals
def time_dirs(case_win: Path) -> list[float]:
    out = []
    for d in case_win.iterdir() if case_win.exists() else []:
        try:
            out.append(float(d.name))
        except ValueError:
            pass
    return sorted(out)


def signal_test(fork: str, mode: str):
    """mode: esi-optswitch | of14-site | of14-controldict"""
    case = f"{WSL_ROOT}/sig_{mode}"
    case_win = WIN_ROOT / f"sig_{mode}"
    if fork == "esi":
        tut, app = "$FOAM_TUTORIALS/incompressible/icoFoam/cavity/cavity", "icoFoam -opt-switch writeNowSignal=10 -opt-switch stopAtWriteNowSignal=12"
    else:
        tut, app = "$FOAM_TUTORIALS/incompressibleFluid/cavity", "foamRun"
    site_setup = ""
    if mode == "of14-site":
        site_setup = f"""mkdir -p {WSL_ROOT}/site/etc
cat > {WSL_ROOT}/site/etc/configDict <<'EOF'
FoamFile {{ format ascii; class dictionary; object configDict; }}
OptimisationSwitches {{ writeNowSignal 10; stopAtWriteNowSignal 12; }}
EOF
export WM_PROJECT_SITE={WSL_ROOT}/site
"""
    cd_patch = ""
    if mode == "of14-controldict":
        cd_patch = "printf '\\nOptimisationSwitches { writeNowSignal 10; stopAtWriteNowSignal 12; }\\n' >> system/controlDict\n"
    body = prelude(fork) + f"""set -e
rm -rf {case}; cp -r {tut} {case}; cd {case}
foamDictionary -entry endTime -set 1000 system/controlDict >/dev/null
foamDictionary -entry writeControl -set timeStep system/controlDict >/dev/null
foamDictionary -entry writeInterval -set 10000000 system/controlDict >/dev/null
{cd_patch}blockMesh > log.blockMesh 2>&1
{site_setup}setsid bash -c 'exec {app} > log.solver 2>&1' < /dev/null > /dev/null 2>&1 &
pid=$!
sleep 0.5
echo "PGID=$(ps -o pgid= $pid | tr -d ' ') PID=$pid START=$(cut -d' ' -f22 /proc/$pid/stat)"
"""
    rc, out = wsl(body, name=f"sig_launch_{mode}", timeout=120)
    pg = next((ln for ln in out.splitlines() if ln.startswith("PGID=")), None)
    if rc != 0 or not pg:
        check(f"signals {mode}: launched", False, out[-300:])
        return
    pgid = pg.split()[0].split("=")[1]
    time.sleep(4)
    before = time_dirs(case_win)
    rc, out = wsl(f"kill -USR1 -{pgid}; sleep 3; kill -0 -{pgid} && echo ALIVE\n", name="sig_usr1", timeout=60)
    after1 = time_dirs(case_win)
    wrote = [t for t in after1 if t not in before and t > 0]
    alive = "ALIVE" in out
    check(f"signals {mode}: SIGUSR1 writes a time dir and keeps running", bool(wrote) and alive,
          f"new={wrote[:3]} alive={alive}")
    rc, out = wsl(f"kill -USR2 -{pgid}; for i in $(seq 1 40); do kill -0 -{pgid} 2>/dev/null || {{ echo EXITED; break; }}; sleep 0.5; done\n",
                  name="sig_usr2", timeout=60)
    after2 = time_dirs(case_win)
    wrote2 = [t for t in after2 if t not in after1 and t > 0]
    log = (case_win / "log.solver").read_text(errors="replace") if (case_win / "log.solver").exists() else ""
    check(f"signals {mode}: SIGUSR2 writes and exits", "EXITED" in out and bool(wrote2),
          f"new={wrote2[:3]} exited={'EXITED' in out} log_tail={log.strip().splitlines()[-2:] if log else None}")
    # make sure nothing is left running
    wsl(f"kill -KILL -{pgid} 2>/dev/null; true\n", name="sig_cleanup", timeout=30)


if 3 in ONLY:
    signal_test("esi", "esi-optswitch")
    signal_test("of14", "of14-site")
    signal_test("of14", "of14-controldict")

# ------------------------------------------------------------------ 4. detached run vs. client lifetime
if 4 in ONLY:
    def detached_ticker(tag: str) -> str:
        tick = f"{WSL_ROOT}/ticks_{tag}.txt"
        body = f"""rm -f {tick}
setsid bash -c 'for i in $(seq 1 120); do date +%s >> {tick}; sleep 1; done' < /dev/null > /dev/null 2>&1 &
echo started
"""
        wsl(body, name=f"ticker_{tag}", timeout=60)
        return tick

    def ticks(tag: str) -> int:
        p = WIN_ROOT / f"ticks_{tag}.txt"
        return len(p.read_text().split()) if p.exists() else 0

    # (a) no anchor: the launching wsl.exe has exited; do ticks keep coming for 75 s?
    detached_ticker("noanchor")
    n0 = ticks("noanchor")
    time.sleep(75)
    n1 = ticks("noanchor")
    still_running = DISTRO in running_distros()
    report["noanchor"] = {"ticks_at_launch": n0, "ticks_after_75s": n1, "distro_running": still_running}
    check("(info) detached job without anchor keeps running 75 s after its wsl.exe exits",
          n1 >= 60, f"ticks {n0}->{n1}, distro running={still_running}")

    # (b) with anchor client
    anchor = subprocess.Popen(["wsl.exe", "-d", DISTRO, "--cd", "/", "-e", "sleep", "infinity"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW)
    time.sleep(2)
    detached_ticker("anchor")
    a0 = ticks("anchor")
    time.sleep(75)
    a1 = ticks("anchor")
    anchor.terminate()  # ends the client only; the distro is never shut down by us
    report["anchor"] = {"ticks_at_launch": a0, "ticks_after_75s": a1}
    check("detached job with anchor keeps running 75 s", a1 >= 60, f"ticks {a0}->{a1}")

# ------------------------------------------------------------------ 5. decomposed case loads in VTK
if 5 in ONLY:
    case = f"{WSL_ROOT}/decomp_cavity"
    body = prelude("esi") + f"""set -e
rm -rf {case}; cp -r $FOAM_TUTORIALS/incompressible/icoFoam/cavity/cavity {case}; cd {case}
cat > system/decomposeParDict <<'EOF'
FoamFile {{ format ascii; class dictionary; object decomposeParDict; }}
numberOfSubdomains 4;
method simple;
coeffs {{ n (2 2 1); }}
EOF
blockMesh > log.blockMesh 2>&1
decomposePar > log.decomposePar 2>&1
mpirun --allow-run-as-root -np 4 --bind-to none icoFoam -parallel > log.icoFoam 2>&1
ls -d processor*
"""
    rc, out = wsl(body, name="decomp", timeout=600)
    check("decomposePar + mpirun -np 4 icoFoam -parallel (as root)", rc == 0 and "processor3" in out, out.strip()[-160:])
    if rc == 0:
        import vtk

        r = vtk.vtkPOpenFOAMReader()
        r.SetFileName(str(WIN_ROOT / "decomp_cavity" / "system" / "controlDict"))
        r.SetCaseType(0)  # decomposed
        r.CreateCellToPointOff()
        r.UpdateInformation()
        tv = r.GetTimeValues()
        times = [tv.GetValue(i) for i in range(tv.GetNumberOfTuples())] if tv else []
        r.SetCellArrayStatus("p", 1)
        r.UpdateTimeStep(times[-1] if times else 0.0)
        r.Update()
        it = r.GetOutput().NewIterator()
        it.InitTraversal()
        ncells = 0
        while not it.IsDoneWithTraversal():
            ds = it.GetCurrentDataObject()
            if ds is not None and ds.IsA("vtkUnstructuredGrid"):
                ncells += ds.GetNumberOfCells()
            it.GoToNextItem()
        check("decomposed case loads with vtkPOpenFOAMReader(CaseType=0)", ncells == 400,
              f"times={times} cells={ncells} (expect 400)")

out_dir = Path(__file__).parent / "out"
out_dir.mkdir(exist_ok=True)
report["all_ok"] = all(c["ok"] for k, c in report["checks"].items() if not k.startswith("(info)"))
(out_dir / f"spike_c_{args.only.replace(',', '')}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print("ALL OK" if report["all_ok"] else "SOME CHECKS FAILED")
