"""Generate the bash that launches, runs and guards an OpenFOAM run inside WSL (ADR 0006).

Files in the run dir (``.aethercfd/local/runs/<id>/``, readable from Windows):
  runner.sh   steps, run under ``setsid -f`` (its PID == process group == session)
  guard.sh    in-distro watchdog in its *own* session (survives the GUI; stops runaway runs)
  runner.pid  "<pid> <starttime-jiffies> <boot_id>" written by the runner itself (handshake)
  steps.jsonl one JSON line per step start/end      solver.pid  pid of the current solver/mpirun
  stats.jsonl guard samples every few seconds       exit_code   written when the runner ends
  guard_stop.json  why the guard stopped the run     stop_requested  user asked to stop (skip rest)

Signals: SIGUSR1/SIGUSR2 are sent ONLY to the solver (or mpirun, which forwards them to its ranks),
never to the whole group: bash's default action for USR1/USR2 is to terminate, which would kill the
runner itself.
"""

from __future__ import annotations

import shlex

from aethercfd.backend.wsl.forks import prelude
from aethercfd.runs.spec import RunSpec, RunStep, StepKind

q = shlex.quote


def _step_cmd(spec: RunSpec, step: RunStep) -> list[str]:
    argv = [step.app, *step.args]
    if step.kind is StepKind.SOLVER:
        argv += list(spec.signal_args())
    if step.parallel:
        argv = ["mpirun", "-np", str(spec.nprocs), "--bind-to", "none"] + (
            ["--allow-run-as-root"] if spec.fork.mpi_run_as_root else []) + argv + ["-parallel"]
    return argv


def runner_sh(spec: RunSpec) -> str:
    cap_blocks = int(spec.file_cap_gb * 1024 * 1024)  # bash ulimit -f counts 1024-byte blocks
    lines = [
        "#!/bin/bash",
        f"# AetherCFD run {spec.run_id} -- generated; do not edit",
        f"RUN_DIR={q(spec.run_dir_wsl)}",
        f"LRUN={q(spec.linux_run_dir)}",
        f"CASE={q(spec.case_wsl)}",
        'mkdir -p "$LRUN"',
        'echo "$$ $(cut -d" " -f22 /proc/$$/stat) $(cat /proc/sys/kernel/random/boot_id)" > "$RUN_DIR/runner.pid"',
        prelude(spec.fork).rstrip("\n"),
    ]
    if spec.uses_site_dir:
        lines += [
            'mkdir -p "$LRUN/site/etc"',
            "cat > \"$LRUN/site/etc/configDict\" <<'AETHER_EOF'",
            "FoamFile { format ascii; class dictionary; object configDict; }",
            "OptimisationSwitches { writeNowSignal 10; stopAtWriteNowSignal 12; }",
            "AETHER_EOF",
            'export WM_PROJECT_SITE="$LRUN/site"',
        ]
    for k, v in spec.env.items():
        lines.append(f"export {k}={q(v)}")
    lines += [
        'cd "$CASE" || { echo 96 > "$RUN_DIR/exit_code"; exit 96; }',
        "trap '' USR1 USR2  # the runner must survive a mis-targeted write-now signal",
        # TERM (stop/kill from the GUI or the guard): record an exit code before going away, so the
        # monitor can tell "stopped" from "lost". `wait` returns at once when a trapped signal arrives.
        "trap 'echo 143 > \"$RUN_DIR/exit_code\"; exit 143' TERM",
        "step() {",
        '  local idx="$1" kind="$2" log="$3"; shift 3',
        '  if [ -f "$RUN_DIR/stop_requested" ]; then',
        '    echo "{\\"step\\":$idx,\\"state\\":\\"skipped\\",\\"t\\":$(date +%s)}" >> "$RUN_DIR/steps.jsonl"; return 0',
        "  fi",
        '  echo "{\\"step\\":$idx,\\"state\\":\\"running\\",\\"log\\":\\"$log\\",\\"t\\":$(date +%s)}" >> "$RUN_DIR/steps.jsonl"',
        f'  ( ulimit -f {cap_blocks}; trap - USR1 USR2; exec nice -n {spec.nice} "$@" ) > "$log" 2>&1 &',
        "  local pid=$!",
        '  echo "$pid $kind" > "$RUN_DIR/solver.pid"',
        '  wait "$pid"; local rc=$?',
        "  while [ $rc -gt 128 ] && kill -0 \"$pid\" 2>/dev/null; do wait \"$pid\"; rc=$?; done  # resumed after STOP",
        '  rm -f "$RUN_DIR/solver.pid"',
        '  echo "{\\"step\\":$idx,\\"state\\":\\"done\\",\\"rc\\":$rc,\\"t\\":$(date +%s)}" >> "$RUN_DIR/steps.jsonl"',
        "  return $rc",
        "}",
    ]
    for i, s in enumerate(spec.steps):
        argv = " ".join(q(a) for a in _step_cmd(spec, s))
        lines.append(f'step {i} {s.kind.value} {q(s.log)} {argv} || {{ rc=$?; echo $rc > "$RUN_DIR/exit_code"; '
                     f"exit $rc; }}")
    lines.append('echo 0 > "$RUN_DIR/exit_code"')
    return "\n".join(lines) + "\n"


def guard_sh(spec: RunSpec, *, interval_s: int = 5, log_cap_gb: float = 10.0, reserve_gb: float = 20.0,
             reserve_fraction: float = 0.10) -> str:
    """In-distro watchdog: samples resources and stops the run if a hard limit is crossed."""
    cap = int(log_cap_gb * 1024**3)
    return "\n".join([
        "#!/bin/bash",
        f"# AetherCFD guard for run {spec.run_id} -- generated; do not edit",
        f"RUN_DIR={q(spec.run_dir_wsl)}",
        f"CASE={q(spec.case_wsl)}",
        f"CAP={cap}",
        f"RESERVE_GB={reserve_gb}",
        f"RESERVE_FRAC={reserve_fraction}",
        "for i in $(seq 1 100); do [ -s \"$RUN_DIR/runner.pid\" ] && break; sleep 0.1; done",
        'read -r PGID _ _ < "$RUN_DIR/runner.pid"',
        "stop_run() {",
        '  printf \'{"reason":"%s","t":%s}\\n\' "$1" "$(date +%s)" > "$RUN_DIR/guard_stop.json"',
        '  touch "$RUN_DIR/stop_requested"',
        '  kill -TERM -"$PGID" 2>/dev/null; sleep 15; kill -KILL -"$PGID" 2>/dev/null',
        "}",
        'while kill -0 -"$PGID" 2>/dev/null && [ ! -f "$RUN_DIR/exit_code" ]; do',
        '  LOG=$(ls -t "$CASE"/log.* 2>/dev/null | head -1)',
        '  LSZ=$( [ -n "$LOG" ] && stat -c %s "$LOG" || echo 0)',
        '  read -r DTOT DAV <<< "$(df -B1 --output=size,avail "$CASE" | tail -1)"',
        "  MEMAV=$(awk '/MemAvailable/{print $2*1024}' /proc/meminfo)",
        "  MEMTOT=$(awk '/MemTotal/{print $2*1024}' /proc/meminfo)",
        '  RSS=$(ps -o rss= -g "$PGID" 2>/dev/null | awk \'{s+=$1} END{print s*1024}\')',
        '  printf \'{"t":%s,"log":"%s","log_bytes":%s,"disk_total":%s,"disk_avail":%s,"mem_avail":%s,'
        '"mem_total":%s,"rss":%s}\\n\' "$(date +%s)" "$LOG" "$LSZ" "$DTOT" "$DAV" "$MEMAV" "$MEMTOT" "${RSS:-0}" '
        '>> "$RUN_DIR/stats.jsonl"',
        '  if [ "$LSZ" -ge "$CAP" ]; then stop_run "log $LOG reached $LSZ bytes"; break; fi',
        "  RES=$(awk -v t=\"$DTOT\" -v g=\"$RESERVE_GB\" -v f=\"$RESERVE_FRAC\" "
        "'BEGIN{r=g*1073741824; x=t*f; if (x>r) r=x; printf \"%d\", r}')",
        '  if [ "$DAV" -le "$RES" ]; then stop_run "disk free $DAV below reserve $RES"; break; fi',
        f"  sleep {interval_s}",
        "done",
        "",
    ])


def launch_sh(spec: RunSpec) -> str:
    """Starts runner + guard detached, waits for the runner's handshake, prints its identity."""
    return "\n".join([
        f"RUN_DIR={q(spec.run_dir_wsl)}",
        'rm -f "$RUN_DIR/runner.pid" "$RUN_DIR/exit_code" "$RUN_DIR/stop_requested" "$RUN_DIR/guard_stop.json"',
        'setsid -f bash "$RUN_DIR/runner.sh" < /dev/null > "$RUN_DIR/runner.out" 2>&1',
        "for i in $(seq 1 100); do [ -s \"$RUN_DIR/runner.pid\" ] && break; sleep 0.05; done",
        'if [ ! -s "$RUN_DIR/runner.pid" ]; then echo "AETHERCFD: runner did not start" >&2; '
        'cat "$RUN_DIR/runner.out" >&2; exit 1; fi',
        'setsid -f bash "$RUN_DIR/guard.sh" < /dev/null > "$RUN_DIR/guard.out" 2>&1',
        'echo "RUNNER $(cat "$RUN_DIR/runner.pid")"',
        "",
    ])


def control_sh(action: str, spec_run_dir_wsl: str, pgid: int) -> str:
    """Bash for one control action. USR1/USR2 go to the solver/mpirun pid only (see module doc)."""
    rd = q(spec_run_dir_wsl)
    head = f"RUN_DIR={rd}\nPGID={pgid}\n"
    solver = ('read -r SPID SKIND < "$RUN_DIR/solver.pid" 2>/dev/null || '
              '{ echo "AETHERCFD: no solver is running" >&2; exit 3; }\n'
              '[ "$SKIND" = solver ] || { echo "AETHERCFD: current step is not a solver" >&2; exit 3; }\n')
    body = {
        "write_now": solver + 'kill -USR1 "$SPID"\n',
        "stop_write": 'touch "$RUN_DIR/stop_requested"\n' + solver + 'kill -USR2 "$SPID"\n',
        "pause": 'kill -STOP -"$PGID"\n',
        "resume": 'kill -CONT -"$PGID"\n',
        "terminate": 'touch "$RUN_DIR/stop_requested"\nkill -CONT -"$PGID" 2>/dev/null\nkill -TERM -"$PGID"\n',
        "kill": 'touch "$RUN_DIR/stop_requested"\nkill -CONT -"$PGID" 2>/dev/null\nkill -TERM -"$PGID" 2>/dev/null\n'
                'for i in $(seq 1 30); do kill -0 -"$PGID" 2>/dev/null || exit 0; sleep 0.5; done\n'
                'kill -KILL -"$PGID"\n',
        "alive": 'if kill -0 -"$PGID" 2>/dev/null; then echo "ALIVE $(cut -d" " -f22 /proc/$PGID/stat 2>/dev/null) '
                 '$(cat /proc/sys/kernel/random/boot_id)"; else echo DEAD; fi\n',
    }[action]
    return head + body
