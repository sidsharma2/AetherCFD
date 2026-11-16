"""How AetherCFD reaches a cluster (ADR 0007): OpenSSH + rsync *inside the WSL distro*, driven through
``WslRunner`` (still the only code that executes ``wsl.exe``).

Every call is non-interactive (``BatchMode=yes``): a missing key or an expired MFA session fails at once
(exit 255) instead of hanging on a prompt. ``ControlMaster=auto`` reuses one connection for all calls; on an
MFA cluster the user opens it once in a console (:meth:`WslSshTransport.master_console_argv`).

Remote scripts are base64-encoded into the local script and decoded on the login node into
``bash -l -c 'script=$(cat); exec </dev/null; eval "$script"'``: no quoting layers, and ``module`` from
the login environment. Replies are parsed by marker lines, so login banners cannot corrupt them.

:class:`Transport` is the seam: the unit tests replace it with a fake cluster on the local file system.
"""

from __future__ import annotations

import base64
import posixpath
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from aethercfd.backend.wsl.runner import ScriptResult, WslRunner
from aethercfd.core.clusters import ClusterProfile
from aethercfd.core.paths import win_to_wsl
from aethercfd.runs.cluster.scheduler import SchedulerError, get_scheduler

q = shlex.quote

UNREACHABLE = 255  # ssh's own exit status for connection/authentication failures
NO_SSH = 254  # ssh or rsync missing in the WSL distro
_BOOT = 'script=$(cat); exec </dev/null; eval "$script"'
_TIME_RE = r"^[-+]?[0-9.]+([eE][-+]?[0-9]+)?$"
RUN_FILES = ("steps.jsonl", "exit_code", "guard_stop.json", "runner.started", "job.out")


def unreachable(res: ScriptResult) -> bool:
    return res.returncode in (UNREACHABLE, NO_SSH)


def ssh_hint(err: str) -> str:
    """What the user can do about a failed non-interactive SSH call ("" when the error says nothing known)."""
    if "Host key verification failed" in err:
        return " (unknown host key: use Run > Clusters… > Connect (interactive) once to check and accept it)"
    if "Permission denied" in err or "keyboard-interactive" in err:
        return (" (no usable key: set identity_file, load ssh-agent, or sign in with Run > Clusters… > "
                "Connect (interactive))")
    if "Could not resolve hostname" in err or "Name or service not known" in err:
        return " (unknown host name: check the profile's host, and that this PC is online)"
    if "timed out" in err or "No route to host" in err or "Connection refused" in err:
        return " (no answer from the login node: check the host, the VPN and the jump host)"
    return ""


def failure_text(res: ScriptResult, *, limit: int = 300) -> str:
    """The last line of a failed call's output, plus a hint for the usual SSH failures."""
    err = (res.stderr or res.stdout).strip()
    if res.timed_out:
        return "no reply before the time limit"
    last = err.splitlines()[-1].strip() if err else f"exit code {res.returncode}, no output"
    return last[-limit:] + ssh_hint(err)


class Transport(Protocol):
    def run(self, script: str, *, timeout_s: float = 120) -> ScriptResult:
        """Run a bash script on the login node."""
        ...

    def put_text(self, remote_path: str, text: str, *, timeout_s: float = 120) -> ScriptResult: ...

    def upload_case(self, local_case: Path, remote_case: str, *, start_time: str | None,
                    timeout_s: float = 3600) -> ScriptResult:
        """system/, constant/, the start time and top-level files (no log.*) -> remote case."""
        ...

    def pull_live(self, remote_run: str, remote_case: str, local_run: Path, *, timeout_s: float = 120) -> ScriptResult:
        """Remote run files (RUN_FILES) -> local_run/, remote log.* -> local_run/case/; appends only new bytes."""
        ...

    def download_results(self, remote_case: str, local_case: Path, *, skip_newest: bool, processor_dirs: bool,
                         timeout_s: float = 3600) -> ScriptResult:
        """Time dirs, postProcessing/, log.* (and processor*/) -> the user's case. Never system/ or constant/."""
        ...


def remote_command(profile: ClusterProfile) -> str:
    return ("bash -l -c " if profile.login_shell else "bash --noprofile --norc -c ") + q(_BOOT)


@dataclass
class WslSshTransport:
    profile: ClusterProfile
    runner: WslRunner

    # ------------------------------------------------------------------ ssh argv
    def ssh_options(self, *, batch: bool = True) -> list[str]:
        p = self.profile
        o = ["-o", f"ConnectTimeout={p.connect_timeout_s}", "-o", "ServerAliveInterval=30",
             "-o", "ServerAliveCountMax=4"]
        if batch:
            o = ["-o", "BatchMode=yes", *o]
        if p.port != 22:
            o += ["-p", str(p.port)]
        if p.identity_file:
            o += ["-i", p.identity_file, "-o", "IdentitiesOnly=yes"]
        if p.jump_host:
            o += ["-J", p.jump_host]
        if p.use_control_master:
            o += ["-o", f"ControlPath=~/.ssh/aethercfd-{p.name}-%C"]
            if batch:
                o += ["-o", "ControlMaster=auto", "-o", f"ControlPersist={p.control_persist}"]
        return o

    def _prelude(self) -> str:
        ssh = " ".join(q(a) for a in ["ssh", *self.ssh_options()])
        return "\n".join([
            "set -o pipefail",
            f"for c in ssh rsync base64; do command -v $c >/dev/null || {{ echo \"AETHERCFD: $c is not installed in "
            f"the WSL distro (needed for cluster runs)\" >&2; exit {NO_SSH}; }}; done",
            'mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"',
            f"DEST={q(self.profile.destination)}",
            f"RSH={q(ssh)}",
            f"rssh() {{ {ssh} \"$@\"; }}",
            "",
        ])

    def _exec(self, body: str, timeout_s: float) -> ScriptResult:
        return self.runner.run_script(self._prelude() + body, timeout_s=timeout_s)

    @staticmethod
    def _b64(text: str) -> str:
        return base64.b64encode(text.encode("utf-8")).decode("ascii")

    # ------------------------------------------------------------------ Transport
    def run(self, script: str, *, timeout_s: float = 120) -> ScriptResult:
        return self._exec(f"printf '%s' {q(self._b64(script))} | base64 -d | rssh \"$DEST\" "
                          f"{q(remote_command(self.profile))}\n", timeout_s)

    def put_text(self, remote_path: str, text: str, *, timeout_s: float = 120) -> ScriptResult:
        cmd = f"mkdir -p {q(posixpath.dirname(remote_path))} && cat > {q(remote_path)}"
        return self._exec(f"printf '%s' {q(self._b64(text))} | base64 -d | rssh \"$DEST\" {q(cmd)}\n", timeout_s)

    def upload_case(self, local_case: Path, remote_case: str, *, start_time: str | None,
                    timeout_s: float = 3600) -> ScriptResult:
        inc = ["--include=/system/***", "--include=/constant/***"]
        if start_time:
            inc.append(f"--include=/{start_time}/***")
        return self._exec("\n".join([
            f"SRC={q(win_to_wsl(str(local_case)))}",
            f"rssh \"$DEST\" {q(f'mkdir -p {q(remote_case)}')} || exit $?",
            "rsync -a -e \"$RSH\" " + " ".join(q(i) for i in inc)
            + " --exclude='/log.*' --exclude='/*/' \"$SRC/\" " + q(f"{self.profile.destination}:{remote_case}/"),
            "rc=$?",
            f"[ $rc -eq 0 ] && rssh \"$DEST\" {q(f'rm -f {q(remote_case)}/.aethercfd_synced')}",
            "exit $rc",
            "",
        ]), timeout_s)

    def pull_live(self, remote_run: str, remote_case: str, local_run: Path, *, timeout_s: float = 120) -> ScriptResult:
        dest = self.profile.destination
        inc = " ".join(f"--include={f}" for f in RUN_FILES)
        return self._exec("\n".join([
            f"LRUN={q(win_to_wsl(str(local_run)))}",
            'mkdir -p "$LRUN/case"',
            f"rssh \"$DEST\" true || exit {UNREACHABLE}",
            "rc=0",
            f"rsync -a --append -e \"$RSH\" {inc} --exclude='*' {q(f'{dest}:{remote_run}/')} \"$LRUN/\" || rc=$?",
            f"rsync -a --append -e \"$RSH\" --include='log.*' --exclude='*' {q(f'{dest}:{remote_case}/')} "
            "\"$LRUN/case/\" || rc=$?",
            "exit $rc",
            "",
        ]), timeout_s)

    def download_results(self, remote_case: str, local_case: Path, *, skip_newest: bool, processor_dirs: bool,
                         timeout_s: float = 3600) -> ScriptResult:
        skip = "true" if skip_newest else "false"
        lines = [
            f"DST={q(win_to_wsl(str(local_case)))}",
            f"R={q(remote_case)}",
            f"rssh \"$DEST\" true || exit {UNREACHABLE}",
            f"listing=$(rssh \"$DEST\" {q(f'ls -1 {q(remote_case)}')}) || exit $?",
            f"times=$(printf '%s\\n' \"$listing\" | grep -E '{_TIME_RE}' | sort -g)",
            'newest=$(printf "%s\\n" "$times" | tail -1)',
            "rc=0",
            "for t in $times; do",
            f'  if {skip} && [ "$t" = "$newest" ]; then continue; fi',
            '  rsync -a -e "$RSH" "$DEST:$R/$t" "$DST/" || rc=$?',
            "done",
            'if printf "%s\\n" "$listing" | grep -qx postProcessing; then',
            '  rsync -a -e "$RSH" "$DEST:$R/postProcessing" "$DST/" || rc=$?',
            "fi",
            "rsync -a -e \"$RSH\" --include='log.*' --exclude='*' \"$DEST:$R/\" \"$DST/\" || rc=$?",
        ]
        if processor_dirs:
            lines += [
                f"ptimes=$(rssh \"$DEST\" {q(f'ls -1 {q(remote_case)}/processor0 2>/dev/null')} | grep -E "
                f"'{_TIME_RE}' | sort -g)",
                'pnewest=$(printf "%s\\n" "$ptimes" | tail -1)',
                "excl=''",
                f'if {skip} && [ -n "$pnewest" ]; then excl="--exclude=/$pnewest/"; fi',
                'for p in $(printf "%s\\n" "$listing" | grep -E "^processors?[0-9]+$"); do',
                '  rsync -a -e "$RSH" $excl "$DEST:$R/$p" "$DST/" || rc=$?',
                "done",
            ]
        if not skip_newest:
            lines.append(f"[ $rc -eq 0 ] && rssh \"$DEST\" {q(f'touch {q(remote_case)}/.aethercfd_synced')}")
        lines += ["exit $rc", ""]
        return self._exec("\n".join(lines), timeout_s)

    # ------------------------------------------------------------------ interactive first connection
    def master_console_argv(self) -> list[str]:
        """argv (inside the distro) for a console where the user signs in once (password, MFA code, new host key);
        the master connection then stays up in the background for ``control_persist``."""
        p = self.profile
        opts = self.ssh_options(batch=False)
        if p.use_control_master:
            opts += ["-o", "ControlMaster=yes", "-o", f"ControlPersist={p.control_persist}", "-fN"]
        ssh = " ".join(q(a) for a in ["ssh", *opts, p.destination])
        msg = (f"Signed in: AetherCFD can now reach {p.destination} for {p.control_persist}."
               if p.use_control_master else f"Connected to {p.destination}.")
        script = (f'mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"; echo "Connecting to {p.destination} '
                  f'(type your password / one-time code here; AetherCFD never sees them)"; '
                  f"{ssh} && echo {q(msg)}; echo; read -r -p 'Press Enter to close this window. ' _")
        return ["bash", "--noprofile", "--norc", "-c", script]


# ============================================================================ connection test
@dataclass(frozen=True)
class CheckLine:
    ok: bool | None  # None: information only
    text: str


CHECK_BEGIN, CHECK_END = "AETHER_CHECK", "AETHER_END"


def check_script(profile: ClusterProfile) -> str:
    setup = profile.env_setup or ":"
    try:
        cmds = " ".join(get_scheduler(profile.scheduler).commands)
    except SchedulerError:
        cmds = ""
    return "\n".join([
        f"echo {CHECK_BEGIN}",
        'echo "HOST=$(hostname)"',
        f"for c in {cmds} rsync; do",
        '  if command -v "$c" >/dev/null 2>&1; then echo "HAVE=$c"; else echo "MISSING=$c"; fi',
        "done",
        f"WD={profile.work_dir}",  # validated: no spaces or quotes; ~ and $VARS expand here, on the cluster
        'if mkdir -p "$WD" 2>/dev/null && [ -w "$WD" ]; then echo "WORK=$WD"; else echo "WORK_BAD=$WD"; fi',
        f"( set +u; {setup} >/dev/null 2>&1; echo \"WMV=${{WM_PROJECT_VERSION:-}}\"; "
        "echo \"WMD=${WM_PROJECT_DIR:-}\" )",
        f"echo {CHECK_END}",
        "",
    ])


def parse_check(profile: ClusterProfile, res: ScriptResult) -> tuple[list[CheckLine], str | None]:
    """(report lines, resolved absolute work dir or None)."""
    if res.returncode == NO_SSH:
        return [CheckLine(False, (res.stderr or "ssh/rsync missing in the WSL distro").strip())], None
    lines = [ln.strip() for ln in res.stdout.splitlines()]
    if CHECK_BEGIN not in lines or CHECK_END not in lines:
        err = (res.stderr or res.stdout).strip()
        return [CheckLine(False, f"cannot reach {profile.destination}: {err[-400:] or 'no reply'}{ssh_hint(err)}")], None
    body = lines[lines.index(CHECK_BEGIN) + 1:lines.index(CHECK_END)]
    kv: dict[str, list[str]] = {}
    for ln in body:
        if "=" in ln:
            k, v = ln.split("=", 1)
            kv.setdefault(k, []).append(v)
    out = [CheckLine(True, f"connected to {kv.get('HOST', ['?'])[0]} as {profile.destination}")]
    try:
        get_scheduler(profile.scheduler)
    except SchedulerError as exc:
        out.append(CheckLine(False, str(exc)))
    missing = kv.get("MISSING", [])
    if missing:
        sched = [m for m in missing if m != "rsync"]
        if sched:
            out.append(CheckLine(False, f"not found on the login node: {', '.join(sched)}"))
        if "rsync" in missing:
            out.append(CheckLine(False, "rsync is missing on the cluster (needed to transfer cases)"))
    elif kv.get("HAVE"):
        out.append(CheckLine(True, f"{', '.join(kv['HAVE'])} found"))
    work = None
    if kv.get("WORK"):
        work = kv["WORK"][0]
        if not work.startswith("/") or " " in work:
            out.append(CheckLine(False, f"work dir resolves to {work!r} (must be absolute, without spaces)"))
            work = None
        else:
            out.append(CheckLine(True, f"work dir {work} is writable"))
    else:
        out.append(CheckLine(False, f"work dir {kv.get('WORK_BAD', [profile.work_dir])[0]} is not writable"))
    wmv = (kv.get("WMV") or [""])[0]
    if not wmv:
        out.append(CheckLine(False, f"env_setup {profile.env_setup!r} did not load OpenFOAM"))
    elif profile.openfoam_version and wmv != profile.openfoam_version:
        out.append(CheckLine(False, f"env_setup loads OpenFOAM {wmv}, the profile expects {profile.openfoam_version}"))
    else:
        out.append(CheckLine(True, f"OpenFOAM {wmv} at {(kv.get('WMD') or ['?'])[0]}"))
    return out, work


def check_connection(profile: ClusterProfile, runner: WslRunner | None = None, *,
                    transport: Transport | None = None) -> tuple[list[CheckLine], str | None]:
    """Reach the login node non-interactively and check scheduler commands, rsync, the work dir and OpenFOAM."""
    if transport is None:
        if runner is None:
            raise ValueError("check_connection needs the WSL runner (or a transport)")
        transport = WslSshTransport(profile, runner)
    try:
        res = transport.run(check_script(profile), timeout_s=profile.connect_timeout_s + 90)
    except Exception as exc:
        return [CheckLine(False, f"cannot run ssh in the WSL distro: {exc}")], None
    return parse_check(profile, res)


def resolve_work_dir_script(profile: ClusterProfile) -> str:
    return "\n".join([
        f"WD={profile.work_dir}",  # validated; expands on the cluster
        'mkdir -p "$WD" || exit 3',
        'printf "AETHER_WORK=%s\\n" "$WD"',
        "",
    ])


def parse_work_dir(out: str) -> str | None:
    for ln in out.splitlines():
        if ln.startswith("AETHER_WORK="):
            wd = ln.split("=", 1)[1].strip().rstrip("/")
            if wd.startswith("/") and " " not in wd and "'" not in wd and '"' not in wd:
                return wd
    return None
