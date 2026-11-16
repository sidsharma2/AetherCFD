"""Run manager: prepare -> (stage) -> launch -> monitor -> (sync back) -> finish, plus run controls.

Runs are detached Linux processes (ADR 0006). The manager never reads their stdout; it tails log
files, reads the in-distro guard's samples, evaluates the GUI-side guards, and publishes events on
the core EventBus. A GUI crash does not stop a run; :meth:`RunManager.reattach` picks live runs up
again using (pgid, process start time, boot id) to rule out PID reuse.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from aethercfd.backend.wsl.forks import ForkProfile
from aethercfd.backend.wsl.runner import ScriptResult, WslRunner
from aethercfd.case.model import Case
from aethercfd.core.events import BUS, EventBus
from aethercfd.core.paths import win_to_wsl, wsl_to_win
from aethercfd.core.settings import Settings
from aethercfd.core.textio import write_generated_text
from aethercfd.project.journal import new_id
from aethercfd.project.project import Placement, Project
from aethercfd.runs import guards
from aethercfd.runs.logparse import LogParser
from aethercfd.runs.logtail import LogTail
from aethercfd.runs.placement import Stage, resolve_placement, slug
from aethercfd.runs.scripts import control_sh, guard_sh, launch_sh, runner_sh
from aethercfd.runs.spec import RunSpec, RunStep, StepKind

if TYPE_CHECKING:
    from aethercfd.runs.cluster.target import ClusterTarget

log = logging.getLogger(__name__)


class RunState(StrEnum):
    QUEUED = "queued"
    PENDING = "pending"  # submitted to a cluster scheduler, waiting in its queue (ADR 0007)
    STAGING = "staging"
    LAUNCHING = "launching"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    SYNCING = "syncing"
    FINISHED = "finished"
    FAILED = "failed"
    KILLED = "killed"
    LOST = "lost"
    GUARD_STOPPED = "guard_stopped"

    @property
    def active(self) -> bool:
        return self in (RunState.STAGING, RunState.LAUNCHING, RunState.PENDING, RunState.RUNNING, RunState.PAUSED,
                        RunState.STOPPING, RunState.SYNCING)


@dataclass
class RunRecord:
    id: str
    case_root: str  # Windows path of the user's case
    exec_case_wsl: str  # where OpenFOAM runs (in place or stage)
    run_dir: str  # Windows path
    run_dir_wsl: str
    linux_run_dir: str
    fork: dict
    placement: str
    placement_reason: str
    steps: list[dict]
    nprocs: int
    signals_enabled: bool
    created: str
    state: str = RunState.QUEUED.value
    pgid: int | None = None
    start_jiffies: str | None = None
    boot_id: str | None = None
    exit_code: int | None = None
    guard_stop: str | None = None
    stop_reason: str | None = None
    finished_at: str | None = None
    last_synced_at: str | None = None
    target: str = "local"  # "local" (WSL) | "cluster:<profile>" (ADR 0007)
    job_id: str | None = None  # cluster runs: the scheduler's job id
    scheduler_state: str | None = None  # cluster runs: the scheduler's last word (PENDING, RUNNING, TIMEOUT, ...)
    remote: dict | None = None  # cluster runs: profile snapshot, resources, remote case/run dirs

    @property
    def is_cluster(self) -> bool:
        return self.target.startswith("cluster:")

    @property
    def fork_profile(self) -> ForkProfile:
        return ForkProfile.from_json(self.fork)

    @property
    def staged(self) -> bool:
        return self.placement == Placement.STAGED.value

    def save(self) -> None:
        write_generated_text(Path(self.run_dir) / "run.json", json.dumps(asdict(self), indent=2) + "\n")

    @classmethod
    def load(cls, run_dir: Path) -> RunRecord:
        data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})  # a newer app version may add fields


@dataclass
class _Monitor:
    rec: RunRecord
    thread: threading.Thread | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    parser: LogParser = field(default_factory=LogParser)
    tail: LogTail | None = None
    tail_log: str | None = None
    stats_offset: int = 0
    steps_offset: int = 0
    growth: guards.LogGrowthGuard = field(default_factory=guards.LogGrowthGuard)
    repeat: guards.RepetitionGuard = field(default_factory=guards.RepetitionGuard)
    stall: guards.StallGuard = field(default_factory=guards.StallGuard)
    last_sync: float = 0.0
    last_alive_check: float = 0.0
    dead_replies: int = 0  # definite DEAD replies in a row (an unknown reply neither counts nor resets)
    sim_time: float | None = None
    last_pull: float = float("-inf")  # cluster runs: when the remote run files and logs were last mirrored


class RunManager:
    POLL_S = 1.0
    SYNC_EVERY_S = 300.0
    ALIVE_EVERY_S = 30.0
    DEAD_RECHECK_S = 5.0  # after one DEAD, confirm this soon instead of waiting ALIVE_EVERY_S
    DEAD_REPLIES_FOR_LOST = 2
    REVIVE_CLUSTER_DAYS = 14  # reattach asks the scheduler about cluster runs marked lost this recently

    def __init__(self, project: Project, runner: WslRunner, *, settings: Settings | None = None,
                 bus: EventBus | None = None):
        self.project = project
        self.runner = runner
        self.settings = settings or Settings.load()
        self.bus = bus or BUS
        self._monitors: dict[str, _Monitor] = {}
        self._home: str | None = None

    # ------------------------------------------------------------------ helpers
    def _publish(self, topic: str, rec: RunRecord, **payload) -> None:
        self.bus.publish(topic, {"run_id": rec.id, "case": rec.case_root, **payload})

    def _set_state(self, rec: RunRecord, state: RunState, **extra) -> None:
        if not RunState(rec.state).active and rec.state != RunState.QUEUED.value and state.active:
            return  # a finished run never goes back to an active state (late control replies)
        rec.state = state.value
        for k, v in extra.items():
            setattr(rec, k, v)
        rec.save()
        self._publish("run.state", rec, state=state.value)

    def linux_home(self) -> str:
        if self._home is None:
            res = self.runner.run_script('printf "%s" "$HOME"\n', timeout_s=60)
            self._home = res.stdout.strip() or "/root"
        return self._home

    @property
    def cluster(self) -> ClusterTarget:
        """Cluster runs (ADR 0007); created on first use."""
        t = getattr(self, "_cluster", None)
        if t is None:
            from aethercfd.runs.cluster.target import ClusterTarget

            t = self._cluster = ClusterTarget(self)
        return t

    def exec_case_win(self, rec: RunRecord) -> Path:
        """Where to read live results/logs from Windows (UNC for staged runs, the local mirror for cluster runs)."""
        if _is_cluster(rec):
            return self.cluster.exec_case_win(rec)
        if rec.staged:
            return Path(wsl_to_win(rec.exec_case_wsl, distro=self.runner.distro))
        return Path(rec.case_root)

    # ------------------------------------------------------------------ prepare
    def prepare(self, case: Case, fork: ForkProfile, steps: Sequence[RunStep], *,
                placement: Placement = Placement.AUTO, nprocs: int = 1, enable_signals: bool = True,
                heavy_io: bool = False, target: str = "local",
                resources: dict | None = None) -> tuple[RunRecord, RunSpec | None]:
        if target != "local":  # "cluster:<profile>": the batch script is written at launch (needs the cluster)
            return self.cluster.prepare(case, fork, steps, target=target, nprocs=nprocs,
                                        enable_signals=enable_signals, resources=resources or {})
        place, reason = resolve_placement(case, placement, heavy_io=heavy_io)
        run_id = new_id()
        run_dir = self.project.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        home = self.linux_home()
        linux_run_dir = f"{home}/aethercfd/runs/{run_id}"
        exec_case = case.wsl_path
        if place is Placement.STAGED:
            exec_case = f"{home}/aethercfd/stage/{self.project.id}/{slug(case.name)}-{slug(str(case.root))[-24:]}"
        spec = RunSpec(case_wsl=exec_case, fork=fork, steps=tuple(steps), run_dir_wsl=win_to_wsl(str(run_dir)),
                       linux_run_dir=linux_run_dir, run_id=run_id, nprocs=nprocs, enable_signals=enable_signals,
                       file_cap_gb=self.settings.guards.log_hard_cap_gb)
        problems = spec.validate()
        if problems:
            raise ValueError("; ".join(problems))
        g = self.settings.guards
        write_generated_text(run_dir / "runner.sh", runner_sh(spec))
        write_generated_text(run_dir / "guard.sh", guard_sh(spec, log_cap_gb=g.log_hard_cap_gb,
                                                            reserve_gb=g.disk_reserve_gb,
                                                            reserve_fraction=g.disk_reserve_fraction))
        rec = RunRecord(id=run_id, case_root=str(case.root), exec_case_wsl=exec_case, run_dir=str(run_dir),
                        run_dir_wsl=spec.run_dir_wsl, linux_run_dir=linux_run_dir, fork=fork.to_json(),
                        placement=place.value, placement_reason=reason,
                        steps=[{"app": s.app, "args": list(s.args), "kind": s.kind.value, "parallel": s.parallel,
                                "log": s.log} for s in steps],
                        nprocs=nprocs, signals_enabled=enable_signals,
                        created=datetime.now(UTC).isoformat(timespec="seconds"))
        rec.save()
        return rec, spec

    # ------------------------------------------------------------------ launch
    def launch(self, rec: RunRecord, spec: RunSpec | None, *, start_time: str | None = None) -> RunRecord:
        if _is_cluster(rec):
            return self.cluster.launch(rec, start_time=start_time)
        assert spec is not None
        if rec.staged:
            self._set_state(rec, RunState.STAGING)
            st = Stage(src_wsl=win_to_wsl(rec.case_root), stage_wsl=rec.exec_case_wsl)
            res = self.runner.run_script(st.stage_in_sh(start_time), timeout_s=1800)
            if not res.ok:
                self._set_state(rec, RunState.FAILED, stop_reason=f"staging failed: {res.stderr.strip()[:300]}")
                return rec
        self._set_state(rec, RunState.LAUNCHING)
        res = self.runner.run_script(launch_sh(spec), timeout_s=60)
        line = next((ln for ln in res.stdout.splitlines() if ln.startswith("RUNNER ")), None)
        if not res.ok or line is None:
            self._set_state(rec, RunState.FAILED, stop_reason=f"launch failed: {(res.stderr or res.stdout)[:300]}")
            return rec
        _, pid, start, boot = line.split()[:4]
        self._set_state(rec, RunState.RUNNING, pgid=int(pid), start_jiffies=start, boot_id=boot)
        self._start_monitor(rec)
        return rec

    # ------------------------------------------------------------------ controls
    def control(self, run_id: str, action: str) -> tuple[bool, str]:
        rec = self.get(run_id)
        if rec is not None and _is_cluster(rec):
            return self.cluster.control(rec, action)
        if rec is None or rec.pgid is None:
            return False, "unknown or not launched run"
        if action in ("write_now", "stop_write") and not rec.signals_enabled:
            return False, ("this run was started without write-now signals; sending them would kill the solver. "
                           "Use 'stop' (temporary stopAt writeNow) instead")
        before = (rec.state, rec.stop_reason)
        if action in ("stop_write", "terminate", "kill"):
            # before acting: the monitor may finish the run while the control script is still waiting
            self._set_state(rec, RunState.STOPPING, stop_reason=f"user: {action}")
        try:
            res = self.runner.run_script(control_sh(action, rec.run_dir_wsl, rec.pgid), timeout_s=60)
        except Exception as exc:  # wsl.exe missing or stuck: nothing reached the run
            log.exception("%s of run %s failed", action, rec.id)
            if rec.state == RunState.STOPPING.value and (rec.state, rec.stop_reason) != before:
                self._set_state(rec, RunState(before[0]), stop_reason=before[1])
            return False, f"could not reach WSL to {action.replace('_', ' ')} the run: {exc}"
        if action == "pause" and res.ok:
            self._set_state(rec, RunState.PAUSED)
        elif action == "resume" and res.ok:
            self._set_state(rec, RunState.RUNNING)
        return res.ok, (res.stderr or res.stdout).strip()

    def is_alive(self, rec: RunRecord) -> bool | None:
        """True if the run's own process group is alive, False on a definite answer that it is not (DEAD, or the
        pgid now belongs to another process), None when the reply says nothing.

        Cluster runs ask the scheduler instead (same three answers; an unreachable cluster is None)."""
        if _is_cluster(rec):
            return self.cluster.is_alive(rec)
        if rec.pgid is None:
            return False
        try:
            res = self.runner.run_script(control_sh("alive", rec.run_dir_wsl, rec.pgid), timeout_s=30)
        except Exception:
            return None
        raw = res.stdout.replace("\r", "")
        reply = raw.strip()
        out = reply.split()
        complete = raw.rstrip(" \t").endswith("\n")  # the probe always ends its line: without it the reply was cut
        if not res.ok or not complete or "\n" in reply or not (
                reply == "DEAD" or (len(out) == 3 and out[0] == "ALIVE")):
            # an empty, garbled or truncated reply (WSL busy, wsl.exe hiccup, timeout) says nothing about the run:
            # never mark a healthy run "lost" on it (seen live: a 1M-cell run marked lost while all 8 ranks computed).
            # A reply cut inside the boot id ("ALIVE 555 ab") would otherwise read as another boot, i.e. DEAD.
            return None
        if reply == "DEAD":
            return False
        return out[1] == rec.start_jiffies and out[2] == rec.boot_id

    # ------------------------------------------------------------------ registry of runs
    def runs(self) -> list[RunRecord]:
        out = []
        if self.project.runs_dir.exists():
            for d in sorted(self.project.runs_dir.iterdir()):
                if (d / "run.json").exists():
                    try:
                        out.append(RunRecord.load(d))
                    except Exception:
                        log.exception("unreadable run record %s", d)
        return out

    def get(self, run_id: str) -> RunRecord | None:
        m = self._monitors.get(run_id)
        if m is not None:
            return m.rec
        d = self.project.runs_dir / run_id
        return RunRecord.load(d) if (d / "run.json").exists() else None

    def active_for_case(self, case_root: Path) -> list[RunRecord]:
        key = str(Path(case_root))
        return [r for r in self.runs() if r.case_root == key and RunState(r.state).active]

    def reattach(self, *, revive_lost: bool = True) -> list[RunRecord]:
        """After an app restart: resume monitoring of runs that are still alive.

        A run whose probe answers DEAD (or nothing) is monitored too: the monitor only declares it lost after
        :attr:`DEAD_REPLIES_FOR_LOST` definite DEAD replies. With ``revive_lost``, a run already marked lost on this
        WSL boot is re-opened when its own process group (pgid + start time + boot id) is verifiably alive.
        """
        resumed = []
        records = self.runs()
        for rec in records:
            if not RunState(rec.state).active or rec.id in self._monitors:
                continue
            if (Path(rec.run_dir) / "exit_code").exists():
                self._start_monitor(rec)  # will finalise immediately
                resumed.append(rec)
                continue
            alive = self.is_alive(rec)
            self._start_monitor(rec, dead_replies=1 if alive is False else 0)
            if alive is not False:
                resumed.append(rec)
        if revive_lost:
            lost = [r for r in records if r.state == RunState.LOST.value and r.pgid is not None and r.boot_id
                    and r.id not in self._monitors and not (Path(r.run_dir) / "exit_code").exists()]
            boot = self._boot_id() if lost else None
            resumed += [r for r in lost if r.boot_id == boot and self.revive(r)]
            # cluster runs: the scheduler's job id is the proof of identity (no boot id); recent ones only
            recent = datetime.now(UTC).timestamp() - self.REVIVE_CLUSTER_DAYS * 86400
            resumed += [r for r in records if _is_cluster(r) and r.state == RunState.LOST.value and r.job_id
                        and r.id not in self._monitors and not (Path(r.run_dir) / "exit_code").exists()
                        and _created_ts(r) >= recent and self.revive(r)]
        return resumed

    def _boot_id(self) -> str | None:
        try:
            res = self.runner.run_script("cat /proc/sys/kernel/random/boot_id\n", timeout_s=30)
        except Exception:
            return None
        return (res.stdout.strip() or None) if res.ok else None

    def revive(self, rec: RunRecord) -> bool:
        """Re-open a run wrongly marked "lost" (e.g. by an unanswered probe under heavy load) and monitor it again.

        Only when its own process group is verifiably alive: same pgid, process start time and boot id."""
        if rec.state != RunState.LOST.value or rec.id in self._monitors or (Path(rec.run_dir) / "exit_code").exists():
            return False
        if self.is_alive(rec) is not True:
            return False
        log.warning("run %s was marked lost but its processes are alive: monitoring it again", rec.id)
        rec.state, rec.stop_reason = RunState.RUNNING.value, None  # _set_state never re-activates a finished run
        rec.save()
        self._publish("run.state", rec, state=rec.state, revived=True)
        self._start_monitor(rec)
        return True

    def launch_in_background(self, rec: RunRecord, spec: RunSpec | None, *,
                             start_time: str | None = None) -> threading.Thread:
        """:meth:`launch` on a worker thread (a cluster launch uploads and submits over SSH); :meth:`wait` waits
        for it too. A launch that raises ends the run as failed."""
        def go() -> None:
            try:
                self.launch(rec, spec, start_time=start_time)
            except Exception as exc:
                log.exception("launch of run %s failed", rec.id)
                self._set_state(rec, RunState.FAILED, stop_reason=f"launch failed: {exc}"[:400])

        t = threading.Thread(target=go, name=f"launch-{rec.id}", daemon=True)
        self.__dict__.setdefault("_launching", {})[rec.id] = t
        t.start()
        return t

    def wait(self, run_id: str, timeout: float | None = None) -> RunRecord | None:
        t = self.__dict__.get("_launching", {}).get(run_id)
        if t is not None and t.is_alive():
            t0 = time.monotonic()
            t.join(timeout)
            if timeout is not None:
                timeout = max(0.0, timeout - (time.monotonic() - t0))
        m = self._monitors.get(run_id)
        if m and m.thread:
            m.thread.join(timeout)
        return self.get(run_id)

    def shutdown(self) -> None:
        """Stop *monitoring* (runs keep going in WSL)."""
        for m in self._monitors.values():
            m.stop.set()
        for m in self._monitors.values():
            if m.thread:
                m.thread.join(5)

    # ------------------------------------------------------------------ monitoring
    def _start_monitor(self, rec: RunRecord, *, dead_replies: int = 0) -> None:
        m = _Monitor(rec, dead_replies=dead_replies)
        if dead_replies:
            m.last_alive_check = time.monotonic() - self.ALIVE_EVERY_S + self.DEAD_RECHECK_S
        g = self.settings.guards
        m.growth = guards.LogGrowthGuard(warn_mb_s=g.log_warn_mb_per_s, stop_mb_s=g.log_stop_mb_per_s,
                                         hard_cap_bytes=int(g.log_hard_cap_gb * guards.GB))
        self._monitors[rec.id] = m
        m.thread = threading.Thread(target=self._monitor_loop, args=(m,), name=f"run-{rec.id}", daemon=True)
        m.thread.start()

    def _monitor_loop(self, m: _Monitor) -> None:
        rec = m.rec
        run_dir = Path(rec.run_dir)
        while not m.stop.is_set():
            try:
                done = self._tick(m, run_dir)
            except Exception:
                log.exception("monitor tick failed for run %s", rec.id)
                done = False
            if done:
                break
            m.stop.wait(self.POLL_S)

    def _tick(self, m: _Monitor, run_dir: Path) -> bool:
        rec = m.rec
        now = time.monotonic()
        # 0) cluster runs: mirror the remote run files and logs into run_dir (every poll_s); everything below
        # then reads them exactly as it reads a local run's files
        pulled = self.cluster.pull(m, now) if _is_cluster(rec) else True
        # 1) step transitions
        steps_file = run_dir / "steps.jsonl"
        if steps_file.exists():
            with open(steps_file, "rb") as f:
                f.seek(m.steps_offset)
                chunk = f.read()
            m.steps_offset += len(chunk)
            for ln in chunk.decode("utf-8", errors="replace").splitlines():
                try:
                    ev = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                self._publish("run.step_state", rec, **ev)
                if ev.get("state") == "running" and ev.get("log"):
                    m.tail_log = ev["log"]
                    m.tail = LogTail(self.exec_case_win(rec) / ev["log"])
                    m.parser = LogParser()
                    m.repeat = guards.RepetitionGuard()
        # 1b) a tutorial script writes one log per application it runs: follow the newest of them
        script = m.tail_log is not None and any(
            s.get("kind") == "script" and s.get("log") == m.tail_log for s in rec.steps)
        if script or (m.tail_log or "").endswith("Allrun"):
            try:
                case_dir = self.exec_case_win(rec)
                logs = [p for p in case_dir.glob("log.*") if p.name != m.tail_log and p.is_file()]
                newest = max(logs, key=lambda p: p.stat().st_mtime) if logs else None
            except OSError:
                newest = None
            if newest is not None and (m.tail is None or Path(m.tail.path) != newest):
                m.tail = LogTail(newest)
                m.parser = LogParser()
                m.repeat = guards.RepetitionGuard()
        # 2) log progress
        if m.tail is not None:
            lines = m.tail.read_new()
            if lines:
                self._publish("run.log", rec, log=m.tail_log, lines=lines)
                for step in m.parser.feed(lines):
                    m.sim_time = step.time
                    self._publish("run.progress", rec, log=m.tail_log, step=step)
                v = m.repeat.feed(lines)
                if v.level >= guards.Level.WARN:
                    self._on_guard(m, v)
            if pulled:  # a mirrored log grows in bursts: sample its size only right after a pull
                v = m.growth.sample(now, m.tail.size())
                if v.level >= guards.Level.WARN:
                    self._on_guard(m, v)
            v = m.stall.sample(now, m.sim_time)
            if v.level >= guards.Level.REVIEW:
                self._publish("run.guard", rec, level=v.level.name, guard=v.guard, message=v.message)
        # 3) in-distro guard samples
        stats_file = run_dir / "stats.jsonl"
        if stats_file.exists():
            with open(stats_file, "rb") as f:
                f.seek(m.stats_offset)
                chunk = f.read()
            m.stats_offset += len(chunk)
            last = None
            for ln in chunk.decode("utf-8", errors="replace").splitlines():
                try:
                    last = json.loads(ln)
                except json.JSONDecodeError:
                    continue
            if last:
                self._publish("run.stats", rec, **last)
                g = self.settings.guards
                for v in (guards.disk_verdict(int(last.get("disk_avail", 0)), int(last.get("disk_total", 1)),
                                              reserve_gb=g.disk_reserve_gb, reserve_fraction=g.disk_reserve_fraction,
                                              label="run disk"),
                          guards.memory_verdict(int(last.get("mem_avail", 0)), int(last.get("mem_total", 0)),
                                                label="WSL memory")):
                    if v.level >= guards.Level.WARN:
                        self._on_guard(m, v)
        # 4) periodic sync-back of staged (and cluster) results
        if RunState(rec.state) is RunState.RUNNING and (
                (rec.staged and now - m.last_sync > self.SYNC_EVERY_S)
                or (_is_cluster(rec) and self.cluster.sync_due(m, now))):
            m.last_sync = now
            self._sync_back(rec, skip_newest=True)
        # 5) finished?
        exit_file = run_dir / "exit_code"
        if exit_file.exists():
            code = exit_file.read_text().strip()
            if not code:  # `echo $rc > exit_code` creates the file before writing it: read it on the next poll
                return False
            return self._finish(m, run_dir, int(code.split()[0]))
        if now - m.last_alive_check > self.ALIVE_EVERY_S:
            m.last_alive_check = now
            alive = self.is_alive(rec)
            if alive is True:
                m.dead_replies = 0
            elif alive is None:
                log.info("run %s: liveness probe got no usable reply; still monitoring", rec.id)
            else:
                m.dead_replies += 1
            if m.dead_replies >= self.DEAD_REPLIES_FOR_LOST:
                if exit_file.exists():
                    return self._finish(m, run_dir, int((exit_file.read_text().strip() or "1").split()[0]))
                if _is_cluster(rec):  # the scheduler usually says how the job ended (TIMEOUT, NODE_FAIL, ...)
                    return self.cluster.end_without_exit_code(m)
                if rec.stop_reason == "user: kill":
                    self._set_state(rec, RunState.KILLED, finished_at=datetime.now(UTC).isoformat(timespec="seconds"))
                else:
                    self._set_state(rec, RunState.LOST, stop_reason="process disappeared without an exit code")
                return True
            if m.dead_replies:
                # one DEAD is not enough to end a run: confirm it soon (the runner may be writing its exit code)
                m.last_alive_check = now - self.ALIVE_EVERY_S + self.DEAD_RECHECK_S
        return False

    def _on_guard(self, m: _Monitor, v: guards.Verdict) -> None:
        rec = m.rec
        self._publish("run.guard", rec, level=v.level.name, guard=v.guard, message=v.message, detail=v.detail)
        if v.level is guards.Level.STOP and RunState(rec.state) in (RunState.RUNNING, RunState.PAUSED):
            log.warning("guard %s stopping run %s: %s", v.guard, rec.id, v.message)
            action = "stop_write" if rec.signals_enabled else "terminate"
            ok, _ = self.control(rec.id, action)
            if not ok:
                self.control(rec.id, "terminate")
            rec.guard_stop = f"{v.guard}: {v.message}"
            rec.save()

    def _sync_back(self, rec: RunRecord, *, skip_newest: bool) -> bool:
        try:
            if _is_cluster(rec):
                res = self.cluster.sync_back(rec, skip_newest=skip_newest)
            else:
                st = Stage(src_wsl=win_to_wsl(rec.case_root), stage_wsl=rec.exec_case_wsl)
                res = self.runner.run_script(st.sync_back_sh(skip_newest=skip_newest), timeout_s=3600)
        except Exception as exc:  # a stuck or missing wsl.exe must not leave the run in "syncing" forever
            log.exception("copying the results of run %s back failed", rec.id)
            res = ScriptResult(1, "", f"copying the results back failed: {exc}")
        if res.ok:
            rec.last_synced_at = datetime.now(UTC).isoformat(timespec="seconds")
            rec.save()
            self._publish("run.synced", rec, final=not skip_newest)
        else:
            msg = (res.stderr or res.stdout).strip()[:300] or f"copying the results back failed (exit {res.returncode})"
            self._publish("run.guard", rec, level="WARN", guard="sync", message=msg)
        return res.ok

    def _finish(self, m: _Monitor, run_dir: Path, code: int) -> bool:
        rec = m.rec
        if _is_cluster(rec):  # the last log lines and guard_stop.json may not be mirrored yet
            self.cluster.pull(m, time.monotonic(), force=True)
        if m.tail is not None:  # drain the rest of the log
            lines = m.tail.read_new()
            if lines:
                self._publish("run.log", rec, log=m.tail_log, lines=lines)
                for step in m.parser.feed(lines):
                    self._publish("run.progress", rec, log=m.tail_log, step=step)
            last = m.parser.flush()
            if last is not None:
                self._publish("run.progress", rec, log=m.tail_log, step=last)
        if rec.staged or _is_cluster(rec):
            self._set_state(rec, RunState.SYNCING)
            self._sync_back(rec, skip_newest=False)
        gs = run_dir / "guard_stop.json"
        if gs.exists():
            rec.guard_stop = rec.guard_stop or json.loads(gs.read_text()).get("reason")
        if rec.guard_stop:
            state = RunState.GUARD_STOPPED
        elif rec.stop_reason and rec.stop_reason.startswith("user"):
            state = RunState.KILLED if code != 0 else RunState.FINISHED
        else:
            state = RunState.FINISHED if code == 0 else RunState.FAILED
        if _is_cluster(rec) and state is RunState.FAILED and not rec.stop_reason:
            rec.stop_reason = self.cluster.ended_reason(rec)  # e.g. "scheduler: OUT_OF_MEMORY" (None if not known)
        self._set_state(rec, state, exit_code=code, finished_at=datetime.now(UTC).isoformat(timespec="seconds"))
        return True

    def download_results(self, run_id: str) -> bool:
        """Copy a staged or cluster run's results into the case folder again (time folders, postProcessing,
        logs; never system/ or constant/), e.g. after a lost run. Not while the run is active."""
        rec = self.get(run_id)
        if rec is None or not (rec.staged or _is_cluster(rec)) or RunState(rec.state).active:
            return False
        return self._sync_back(rec, skip_newest=False)


def _is_cluster(rec) -> bool:
    return getattr(rec, "is_cluster", False)  # tolerant of record-like stand-ins (tests, older callers)


def _created_ts(rec: RunRecord) -> float:
    try:
        return datetime.fromisoformat(rec.created).timestamp()
    except ValueError:
        return 0.0


def default_steps(case: Case, *, mesh: bool, check: bool, solve: bool, nprocs: int = 1) -> list[RunStep]:
    """Conventional step list for a case (the user can always edit it)."""
    steps: list[RunStep] = []
    if mesh:
        steps.append(RunStep("blockMesh"))
    if check:
        steps.append(RunStep("checkMesh"))
    if solve:
        if nprocs > 1:
            steps.append(RunStep("decomposePar", ("-force",)))
        app = "foamRun" if case.solver_module else (case.application or "foamRun")
        steps.append(RunStep(app, kind=StepKind.SOLVER, parallel=nprocs > 1))
        if nprocs > 1:
            steps.append(RunStep("reconstructPar", ("-latestTime",)))
    return steps
