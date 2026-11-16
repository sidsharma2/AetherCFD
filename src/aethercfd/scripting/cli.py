"""Headless command-line entry point: ``aethercfd doctor|...``.

Everything the GUI can do is meant to be reachable from here too; the GUI is one client of
the command layer, this CLI is another (and the future MCP server is a third).
"""

from __future__ import annotations

import argparse
import os
import sys

from aethercfd import __version__


def cmd_doctor(args: argparse.Namespace) -> int:
    """What works on this PC and what does not, each with a one-line fix (aethercfd.scripting.doctor)."""
    from aethercfd.scripting.doctor import doctor

    return doctor(backend=not args.no_backend, probe=args.probe, quick=args.quick, as_json=args.json)


class TerminalBroker:
    """Approval in a terminal: shows the full plan and asks; --yes approves non-interactively."""

    def __init__(self, assume_yes: bool):
        self.assume_yes = assume_yes

    def request(self, plan):
        from aethercfd.commands.executor import Approval

        print(plan.describe())
        for fc in plan.file_changes:
            if fc.diff:
                print(fc.diff)
        if self.assume_yes:
            return Approval(True, "cli --yes")
        if not sys.stdin.isatty():
            return Approval(False, "cli", "needs approval; rerun with --yes")
        ans = input("Approve? [y/N] ").strip().lower()
        return Approval(ans in ("y", "yes"), "cli")


def _session(args: argparse.Namespace, *, backend: bool = True):
    from aethercfd.scripting.api import Session

    return Session(args.case, create_project=getattr(args, "create_project", False),
                   broker=TerminalBroker(getattr(args, "yes", False)), with_backend=backend)


def cmd_open(args: argparse.Namespace) -> int:
    from aethercfd.case.model import load_case
    from aethercfd.project.project import Placement, Project
    from aethercfd.runs.placement import resolve_placement

    c = load_case(args.case)
    g = c.fork_guess
    print(f"case      {c.root}")
    print(f"solver    {c.solver_label}")
    print(f"fork      {g.family} {g.version_hint or ''} (confidence {g.confidence:.0%})")
    for e in g.evidence:
        print(f"            - {e}")
    print(f"times     {len(c.times)} ({c.times[0][1]} .. {c.latest_time})" if c.times else "times     none")
    print(f"mesh      {c.n_cells or 'not generated'} cells, regions: {[r.name or '(single)' for r in c.regions]}")
    for r in c.regions:
        kind = "axisymmetric wedge" if r.is_axisymmetric else ("2D (empty)" if r.is_2d else "3D")
        print(f"            {r.name or '(single)'}: {kind}, {len(r.patches)} patches")
    print(f"parallel  {'decomposed, ' + str(c.n_processors) + ' processors' if c.decomposed else 'reconstructed'}")
    place, why = resolve_placement(c, Placement.AUTO)
    print(f"placement {place.value} ({why})")
    if c.generated_by:
        print(f"generated {c.generated_by}")
    proj = Project.find(c.root)
    print(f"project   {'none (read-only browsing)' if proj is None else f'{proj.mode.value} at {proj.meta_dir}'}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    s = _session(args)
    if s.project is None:
        print("no AetherCFD project here yet; rerun with --create-project", file=sys.stderr)
        return 2
    params = {"preset": args.preset, "nprocs": args.np, "placement": args.placement,
              "enable_signals": not args.no_signals}
    if args.fork:
        params["fork"] = args.fork
    if args.target != "local":
        params["target"] = args.target
        params.update({k: v for k in ("walltime", "nodes", "partition", "account") if (v := getattr(args, k))})
    res = s.do("run.start", case=str(s.root), **params)
    print(res.message)
    if not res.ok:
        return 1
    if args.wait:
        def show(topic, p):
            if topic == "run.state":
                print(f"[state] {p['state']}")
            elif topic == "run.progress" and p["step"].index % max(1, args.every) == 0:
                st = p["step"]
                worst = max(st.residuals.items(), key=lambda kv: kv[1].initial, default=None)
                tail = f" worst residual {worst[0]}={worst[1].initial:.2e}" if worst else ""
                print(f"  t={st.time_text}{tail}")
            elif topic == "run.guard":
                print(f"[guard {p['level']}] {p['message']}")
        s.bus.subscribe("*", show)
        rec = s.runs.wait(res.data)
        print(f"run {rec.id}: {rec.state} (exit {rec.exit_code})")
        return 0 if rec.state == "finished" else 1
    return 0


def cmd_clusters(args: argparse.Namespace) -> int:
    from aethercfd.core.settings import Settings

    st = Settings.load()
    if args.check:
        from aethercfd.backend.wsl.runner import WslRunner
        from aethercfd.runs.cluster.transport import check_connection
        from aethercfd.runs.targets import TargetError, find_profile

        try:
            prof = find_profile(st.clusters, args.check)
        except TargetError as exc:
            print(exc, file=sys.stderr)
            return 2
        lines, _ = check_connection(prof, WslRunner(st.wsl.default_distro, allowed=st.wsl.allowed_distros))
        for ln in lines:
            print(f"{'ok     ' if ln.ok else 'PROBLEM' if ln.ok is False else 'info   '}  {ln.text}")
        return 0 if all(ln.ok is not False for ln in lines) else 1
    if not st.clusters:
        print(f"no cluster profiles yet: add one in the Run panel (Clusters…) or as [[clusters]] in {Settings.path()}")
    for c in st.clusters:
        print(f"{c.target:<24} {c.destination:<32} {c.scheduler}  OpenFOAM {c.openfoam_version or '?'}  "
              f"work {c.work_dir}")
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    s = _session(args)
    if s.runs is None:
        print("no project here")
        return 1
    for r in s.runs.runs():
        steps = " -> ".join(st["app"] for st in r.steps)
        where = f"{r.target} job {r.job_id}" if r.is_cluster else r.placement
        print(f"{r.id}  {r.state:<13} {where:<9} exit={r.exit_code}  {steps}  {r.stop_reason or ''}")
    return 0


def cmd_control(args: argparse.Namespace) -> int:
    s = _session(args)
    res = s.do("run.control", run_id=args.run_id, action=args.action)
    print(res.message)
    return 0 if res.ok else 1


def cmd_set(args: argparse.Namespace) -> int:
    s = _session(args, backend=False)
    if s.project is None:
        print("no AetherCFD project here yet; rerun with --create-project", file=sys.stderr)
        return 2
    res = s.do("dict.set", file=str(s.root / args.file), key=args.key, value=args.value,
               insert_if_missing=args.insert)
    print(res.message or ("ok" if res.ok else "failed"))
    return 0 if res.ok else 1


def cmd_report(args: argparse.Namespace) -> int:
    from aethercfd.results.report import build

    rep = build(args.case, out_dir=args.out, renders=not args.no_render)
    print(f"verdict   {rep.data['verdict']}")
    for r in rep.data["verdict_reasons"]:
        print(f"            - {r}")
    for q in rep.data["qois"]:
        ch = f"{q['change_pct']:.2g} %" if q.get("change_pct") is not None else f"{q['samples']} samples"
        print(f"qoi       {q.get('title') or q['id']}: {q['final']:.5g} {q.get('unit', '')} ({ch})")
    for n in rep.notes:
        print(f"note      {n}")
    print(f"report    {rep.markdown}")
    return {"ready": 0, "not ready": 3, "failed": 1}[rep.data["verdict"]]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aethercfd", description=__doc__)
    p.add_argument("--version", action="version", version=f"aethercfd {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor", help="check what works on this PC (app, GPU, WSL and OpenFOAM, tools, copilot)")
    d.add_argument("--no-backend", action="store_true", help="skip WSL/OpenFOAM checks")
    d.add_argument("--probe", action="store_true", help="boot allowed distros if needed and probe OpenFOAM forks")
    d.add_argument("--quick", action="store_true", help="skip the slow checks (PyTorch import, copilot sign-in)")
    d.add_argument("--json", action="store_true", help="print the checks as JSON")
    d.set_defaults(func=cmd_doctor)

    o = sub.add_parser("open", help="describe a case (read-only)")
    o.add_argument("case")
    o.set_defaults(func=cmd_open)

    r = sub.add_parser("run", help="run OpenFOAM on a case (detached; --wait to follow it)")
    r.add_argument("case")
    r.add_argument("--preset", default="solve", choices=["solve", "check", "mesh", "mesh-check-solve"])
    r.add_argument("--np", type=int, default=1)
    r.add_argument("--placement", default="auto", choices=["auto", "in-place", "staged"])
    r.add_argument("--fork", help="fork id, e.g. esi-v2306 or foundation-14")
    r.add_argument("--no-signals", action="store_true")
    r.add_argument("--create-project", action="store_true")
    r.add_argument("--yes", action="store_true", help="approve plans that need approval")
    r.add_argument("--wait", action="store_true")
    r.add_argument("--every", type=int, default=50, help="with --wait: print every N steps")
    r.add_argument("--target", default="local", help="local (WSL) or cluster:<profile name> (see `clusters`)")
    r.add_argument("--walltime", help="cluster runs: e.g. 12:00:00 (default: the profile's)")
    r.add_argument("--nodes", type=int, help="cluster runs: number of nodes")
    r.add_argument("--partition", help="cluster runs: partition / queue")
    r.add_argument("--account", help="cluster runs: allocation account")
    r.set_defaults(func=cmd_run)

    cl = sub.add_parser("clusters", help="list cluster profiles (settings.toml [[clusters]]); --check NAME tests one")
    cl.add_argument("--check", metavar="NAME", help="reach the cluster non-interactively and report what works")
    cl.set_defaults(func=cmd_clusters)

    rs = sub.add_parser("runs", help="list runs of a case/project")
    rs.add_argument("case")
    rs.set_defaults(func=cmd_runs)

    c = sub.add_parser("control", help="write_now | stop_write | pause | resume | terminate | kill")
    c.add_argument("case")
    c.add_argument("run_id")
    c.add_argument("action", choices=["write_now", "stop_write", "pause", "resume", "terminate", "kill"])
    c.add_argument("--yes", action="store_true")
    c.set_defaults(func=cmd_control)

    st = sub.add_parser("set", help="set a dictionary value (minimal, journalled edit)")
    st.add_argument("case")
    st.add_argument("file", help="relative to the case, e.g. system/controlDict")
    st.add_argument("key", help="e.g. endTime or PIMPLE/nOuterCorrectors")
    st.add_argument("value")
    st.add_argument("--insert", action="store_true", help="add the keyword if missing")
    st.add_argument("--create-project", action="store_true")
    st.set_defaults(func=cmd_set)

    rp = sub.add_parser("report", help="write the run report (verdict, quantities of interest, balances, figures); "
                                       "exit 0 ready, 3 not ready, 1 failed")
    rp.add_argument("case")
    rp.add_argument("--out", help="report folder (default: the project's local/reports/<case>-<time>)")
    rp.add_argument("--no-render", action="store_true", help="skip the ParaView field pictures")
    rp.set_defaults(func=cmd_report)

    # listed for --help only; main() hands everything after "copilot" to aethercfd.copilot.headless
    sub.add_parser("copilot", help="ask the Claude copilot about a case, headless (copilot -h for options)",
                   add_help=False)
    # listed for --help only; main() hands everything after "ml-setup" to aethercfd.ml.envsetup
    sub.add_parser("ml-setup", help="create and check the PyTorch environment for PINNs (ml-setup -h for options)",
                   add_help=False)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["copilot"]:
        from aethercfd.copilot.headless import main as copilot_main

        return int(copilot_main(argv[1:]) or 0)
    if argv[:1] == ["ml-setup"]:
        from aethercfd.ml.envsetup import main as ml_setup_main

        return int(ml_setup_main(argv[1:], prog="aethercfd ml-setup") or 0)
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("aethercfd: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # a missing case, WSL not installed, a read-only file: one line, not a traceback
        if os.environ.get("AETHERCFD_DEBUG"):
            raise
        import logging

        logging.getLogger("aethercfd.cli").debug("command failed", exc_info=True)
        print(f"aethercfd: error: {exc}", file=sys.stderr)
        print("(set AETHERCFD_DEBUG=1 to see the full traceback)", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
