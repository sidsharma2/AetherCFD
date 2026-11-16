"""The batch script a cluster run submits: the cluster version of ``runs/scripts.py:runner_sh`` (ADR 0007).

Same contract as the WSL runner, so the monitor reads both the same way: ``steps.jsonl`` (one line per step
start/end), ``solver.pid``, ``exit_code`` (written when the script ends, 143 on SIGTERM), ``stop_requested``
(skip the remaining steps) and ``guard_stop.json`` (why a limit stopped the run). Differences:

- OpenFOAM is loaded by the profile's ``env_setup`` line and checked against ``openfoam_version``;
- parallel steps start under ``srun`` or ``mpirun`` inside the allocation (never ``--allow-run-as-root``);
- the batch shell traps USR1/USR2 and forwards them to the running *solver* only (``scancel --batch``
  reaches the batch shell; srun/mpirun forward to every rank). A USR2 the user did not ask for is the
  scheduler's pre-walltime signal: it is recorded in ``guard_stop.json``.

Kept separate from ``runner_sh`` on purpose: the local runner stays byte-identical (live runs depend on it).
"""

from __future__ import annotations

import shlex

from aethercfd.core.clusters import ClusterProfile
from aethercfd.runs.cluster.scheduler import JobRequest, Scheduler
from aethercfd.runs.spec import RunSpec, RunStep, StepKind

q = shlex.quote


def step_argv(spec: RunSpec, step: RunStep, profile: ClusterProfile) -> list[str]:
    argv = [step.app, *step.args]
    if step.kind is StepKind.SOLVER:
        argv += list(spec.signal_args())
    if step.parallel:
        extra = shlex.split(profile.launcher_args) if profile.launcher_args else []
        if profile.mpi_launcher == "srun":
            argv = ["srun", "-n", str(spec.nprocs), *extra, *argv, "-parallel"]
        else:
            argv = ["mpirun", "-np", str(spec.nprocs), *extra, *argv, "-parallel"]
    return argv


def batch_script(spec: RunSpec, profile: ClusterProfile, scheduler: Scheduler, job: JobRequest) -> str:
    """``spec.case_wsl`` / ``spec.run_dir_wsl`` hold the *remote* case and run dirs here."""
    cap_blocks = int(spec.file_cap_gb * 1024 * 1024)  # bash ulimit -f counts 1024-byte blocks
    ver = profile.openfoam_version
    lines = ["#!/bin/bash", *scheduler.header(job),
             f"# AetherCFD run {spec.run_id} on cluster profile {profile.name} -- generated; do not edit",
             f"RUN_DIR={q(spec.run_dir_wsl)}",
             f"CASE={q(spec.case_wsl)}",
             'echo "{\\"job\\":\\"${' + scheduler.job_id_var + ':-}\\",\\"host\\":\\"$(hostname)\\",\\"t\\":$(date +%s)}"'
             ' > "$RUN_DIR/runner.started"',
             "set +u"]
    if profile.env_setup:
        lines.append(profile.env_setup)
    lines += [
        'if [ -z "${WM_PROJECT_DIR:-}" ]; then',
        '  echo "AETHERCFD: env_setup did not load OpenFOAM (WM_PROJECT_DIR is empty)" >&2',
        '  echo 98 > "$RUN_DIR/exit_code"; exit 98',
        "fi",
    ]
    if ver:
        lines += [
            f'if [ "${{WM_PROJECT_VERSION:-}}" != {q(ver)} ]; then',
            f'  echo "AETHERCFD: expected OpenFOAM {ver}, env_setup loaded ${{WM_PROJECT_VERSION:-nothing}}" >&2',
            '  echo 98 > "$RUN_DIR/exit_code"; exit 98',
            "fi",
        ]
    if spec.uses_site_dir:
        lines += [
            'mkdir -p "$RUN_DIR/site/etc"',
            "cat > \"$RUN_DIR/site/etc/configDict\" <<'AETHER_EOF'",
            "FoamFile { format ascii; class dictionary; object configDict; }",
            "OptimisationSwitches { writeNowSignal 10; stopAtWriteNowSignal 12; }",
            "AETHER_EOF",
            'export WM_PROJECT_SITE="$RUN_DIR/site"',
        ]
    for k, v in spec.env.items():
        lines.append(f"export {k}={q(v)}")
    lines += [
        'cd "$CASE" || { echo 96 > "$RUN_DIR/exit_code"; exit 96; }',
        "CUR_PID=; CUR_KIND=",
        'fwd() { if [ "$CUR_KIND" = solver ] && [ -n "$CUR_PID" ]; then kill -"$1" "$CUR_PID" 2>/dev/null; fi; }',
        # a USR2 nobody asked for (no stop_requested yet) is the scheduler's pre-walltime signal
        "on_usr2() {",
        '  if [ ! -f "$RUN_DIR/stop_requested" ]; then',
        "    printf '{\"reason\":\"walltime: stopped before the time limit (latest time written)\",\"t\":%s}\\n' "
        '"$(date +%s)" > "$RUN_DIR/guard_stop.json"',
        '    touch "$RUN_DIR/stop_requested"',
        "  fi",
        "  fwd USR2",
        "}",
        "trap 'fwd USR1' USR1",
        "trap on_usr2 USR2",
        "trap 'echo 143 > \"$RUN_DIR/exit_code\"; exit 143' TERM",
        "step() {",
        '  local idx="$1" kind="$2" log="$3"; shift 3',
        '  if [ -f "$RUN_DIR/stop_requested" ]; then',
        '    echo "{\\"step\\":$idx,\\"state\\":\\"skipped\\",\\"t\\":$(date +%s)}" >> "$RUN_DIR/steps.jsonl"; return 0',
        "  fi",
        '  if [ -s "$log" ]; then mv -n "$log" "$log.$(date -r "$log" +%Y%m%d-%H%M%S)"; fi',
        '  echo "{\\"step\\":$idx,\\"state\\":\\"running\\",\\"log\\":\\"$log\\",\\"t\\":$(date +%s)}" >> "$RUN_DIR/steps.jsonl"',
        f'  ( ulimit -f {cap_blocks}; trap - USR1 USR2; exec "$@" ) > "$log" 2>&1 &',
        "  local pid=$!",
        '  CUR_PID=$pid; CUR_KIND=$kind',
        '  echo "$pid $kind" > "$RUN_DIR/solver.pid"',
        '  wait "$pid"; local rc=$?',
        '  while [ $rc -gt 128 ] && kill -0 "$pid" 2>/dev/null; do wait "$pid"; rc=$?; done  # woken by a trapped signal',
        '  CUR_PID=; CUR_KIND=',
        '  rm -f "$RUN_DIR/solver.pid"',
        '  echo "{\\"step\\":$idx,\\"state\\":\\"done\\",\\"rc\\":$rc,\\"t\\":$(date +%s)}" >> "$RUN_DIR/steps.jsonl"',
        "  return $rc",
        "}",
    ]
    for i, s in enumerate(spec.steps):
        argv = " ".join(q(a) for a in step_argv(spec, s, profile))
        lines.append(f'step {i} {s.kind.value} {q(s.log)} {argv} || {{ rc=$?; echo $rc > "$RUN_DIR/exit_code"; '
                     f"exit $rc; }}")
    lines.append('echo 0 > "$RUN_DIR/exit_code"')
    return "\n".join(lines) + "\n"
