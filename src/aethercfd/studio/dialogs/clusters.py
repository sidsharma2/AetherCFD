"""Clusters…: add, edit, test and sign in to HPC cluster profiles (ADR 0007).

A profile holds no password: authentication is an SSH key or agent inside the WSL distro, or a connection the user
opens once in a console ("Connect (interactive)…", for MFA clusters) that AetherCFD then reuses.
"""

from __future__ import annotations

import logging

from pydantic import ValidationError
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from aethercfd.core.clusters import ClusterProfile
from aethercfd.runs.cluster.scheduler import SCHEDULERS
from aethercfd.studio.theme.style import mono_font
from aethercfd.studio.theme.tokens import T

log = logging.getLogger(__name__)

_TEXT = (("name", "Name", "e.g. narval (the run target becomes cluster:narval)"),
         ("host", "Login host", "login.cluster.example.org, or a Host alias from ~/.ssh/config in WSL"),
         ("user", "User", "empty: ssh's default"),
         ("identity_file", "Key file (in WSL)", "e.g. ~/.ssh/id_ed25519; empty: ssh-agent / ~/.ssh/config"),
         ("jump_host", "Jump host", "optional ProxyJump, e.g. me@gateway.example.org"),
         ("work_dir", "Remote work dir", "e.g. $SCRATCH/aethercfd (no spaces; ~ and $VARS expand on the cluster)"),
         ("env_setup", "Load OpenFOAM", "one line, e.g. module load StdEnv/2023 openfoam/v2306"),
         ("openfoam_version", "OpenFOAM version", "what env_setup loads: v2306, v2412, 11, 12 ..."),
         ("partition", "Partition", "default partition / queue"),
         ("account", "Account", "allocation account, e.g. def-supervisor"),
         ("qos", "QOS", "optional"),
         ("walltime", "Walltime", "default time limit, HH:MM:SS or D-HH:MM:SS"),
         ("mem", "Memory per node", "e.g. 64G; empty: the partition default"),
         ("launcher_args", "Launcher arguments", "e.g. --mpi=pmix"),
         ("control_persist", "Keep connection", "how long a signed-in connection is reused, e.g. 8h"))


class ClusterProfilesDialog(QDialog):
    def __init__(self, services, parent=None):
        super().__init__(parent)
        self.sv = services
        self.setWindowTitle("Clusters")
        self.resize(900, 640)
        self.profiles: list[ClusterProfile] = [p.model_copy() for p in services.settings.clusters]
        self._row = -1
        outer = QVBoxLayout(self)
        note = QLabel("AetherCFD never stores passwords. It connects with an SSH key or agent inside the WSL distro. "
                      "On a cluster with MFA, sign in once with <b>Connect (interactive)…</b>; AetherCFD then "
                      "reuses that connection.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {T.text_dim};")
        outer.addWidget(note)
        split = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(split, 1)
        # --- list
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        ll.addWidget(self.list, 1)
        row = QHBoxLayout()
        add, rm = QPushButton("Add"), QPushButton("Remove")
        add.clicked.connect(self._add)
        rm.clicked.connect(self._remove)
        row.addWidget(add)
        row.addWidget(rm)
        ll.addLayout(row)
        split.addWidget(left)
        # --- form
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        scroll.setWidget(body)
        form = QFormLayout(body)
        self.fields: dict[str, QLineEdit] = {}
        for key, label, tip in _TEXT:
            e = QLineEdit(placeholderText=tip, toolTip=tip)
            self.fields[key] = e
            form.addRow(label, e)
            if key == "jump_host":
                self.port = QSpinBox(minimum=1, maximum=65535, value=22)
                form.addRow("Port", self.port)
            if key == "openfoam_version":
                self.scheduler = QComboBox()
                self.scheduler.addItems(list(SCHEDULERS))
                form.addRow("Scheduler", self.scheduler)
            if key == "walltime":
                self.nodes = QSpinBox(minimum=0, maximum=10000, specialValueText="scheduler decides")
                self.tpn = QSpinBox(minimum=0, maximum=1024, specialValueText="scheduler decides")
                form.addRow("Nodes", self.nodes)
                form.addRow("Tasks per node", self.tpn)
            if key == "mem":
                self.launcher = QComboBox()
                self.launcher.addItems(["srun", "mpirun"])
                form.addRow("MPI launcher", self.launcher)
        self.extra = QPlainTextEdit(placeholderText="extra #SBATCH options, one per line, e.g. --constraint=cascade")
        self.extra.setMaximumHeight(70)
        form.addRow("Extra options", self.extra)
        self.lead = QSpinBox(minimum=0, maximum=7200, value=300, suffix=" s",
                             toolTip="Stop and write this long before the walltime (0: off)")
        form.addRow("Write before walltime", self.lead)
        self.cm = QCheckBox("Reuse one SSH connection (ControlMaster; needed for MFA clusters)")
        self.cm.setChecked(True)
        form.addRow("", self.cm)
        rl.addWidget(scroll, 1)
        btns = QHBoxLayout()
        self.test_btn = QPushButton("Test connection")
        self.test_btn.clicked.connect(self._test)
        self.connect_btn = QPushButton("Connect (interactive)…")
        self.connect_btn.setToolTip("Opens a console: type your password / one-time code there. The connection then "
                                    "stays open in the background for 'Keep connection'.")
        self.connect_btn.clicked.connect(self._connect)
        btns.addWidget(self.test_btn)
        btns.addWidget(self.connect_btn)
        btns.addStretch(1)
        rl.addLayout(btns)
        self.report = QPlainTextEdit(readOnly=True)
        self.report.setFont(mono_font(9))
        self.report.setMaximumHeight(140)
        rl.addWidget(self.report)
        split.addWidget(right)
        split.setSizes([220, 680])
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)
        self.list.currentRowChanged.connect(self._select)
        self._fill_list()
        if self.profiles:
            self.list.setCurrentRow(0)
        self._enable()

    # ------------------------------------------------------------------ form <-> profile
    def _fill_list(self) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for p in self.profiles:
            self.list.addItem(f"{p.name}  ({p.destination})")
        self.list.blockSignals(False)

    def _enable(self) -> None:
        on = 0 <= self._row < len(self.profiles)
        for w in (*self.fields.values(), self.port, self.scheduler, self.nodes, self.tpn, self.launcher, self.extra,
                  self.lead, self.cm, self.test_btn, self.connect_btn):
            w.setEnabled(on)

    def _show(self, p: ClusterProfile) -> None:
        for key, e in self.fields.items():
            e.setText(str(getattr(p, key)))
        self.port.setValue(p.port)
        self.scheduler.setCurrentText(p.scheduler)
        self.nodes.setValue(p.nodes)
        self.tpn.setValue(p.tasks_per_node)
        self.launcher.setCurrentText(p.mpi_launcher)
        self.extra.setPlainText("\n".join(p.sbatch_extra))
        self.lead.setValue(p.write_before_walltime_s)
        self.cm.setChecked(p.use_control_master)

    def _read(self) -> ClusterProfile:
        """The form as a validated profile (raises ValidationError)."""
        base = self.profiles[self._row].model_dump()
        base.update({k: e.text().strip() for k, e in self.fields.items()})
        base.update(port=self.port.value(), scheduler=self.scheduler.currentText(), nodes=self.nodes.value(),
                    tasks_per_node=self.tpn.value(), mpi_launcher=self.launcher.currentText(),
                    sbatch_extra=[ln.strip() for ln in self.extra.toPlainText().splitlines() if ln.strip()],
                    write_before_walltime_s=self.lead.value(), use_control_master=self.cm.isChecked())
        return ClusterProfile.model_validate(base)

    def _commit(self) -> bool:
        if not 0 <= self._row < len(self.profiles):
            return True
        try:
            self.profiles[self._row] = self._read()
        except ValidationError as exc:
            msgs = "\n".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            QMessageBox.warning(self, "Cluster profile", msgs)
            return False
        it = self.list.item(self._row)
        if it is not None:
            it.setText(f"{self.profiles[self._row].name}  ({self.profiles[self._row].destination})")
        return True

    def _select(self, row: int) -> None:
        if row == self._row:
            return
        if not self._commit():
            self.list.blockSignals(True)
            self.list.setCurrentRow(self._row)
            self.list.blockSignals(False)
            return
        self._row = row
        if 0 <= row < len(self.profiles):
            self._show(self.profiles[row])
        self.report.clear()
        self._enable()

    def _add(self) -> None:
        if not self._commit():
            return
        names = {p.name for p in self.profiles}
        n = next(f"cluster{i}" for i in range(1, 1000) if f"cluster{i}" not in names)
        self.profiles.append(ClusterProfile(name=n, host="login.example.org"))
        self._fill_list()
        self._row = -1
        self.list.setCurrentRow(len(self.profiles) - 1)

    def _remove(self) -> None:
        if not 0 <= self._row < len(self.profiles):
            return
        p = self.profiles[self._row]
        if QMessageBox.question(self, "Remove cluster", f"Remove the profile {p.name}? Runs already submitted with "
                                                        "it keep their own copy and can still be followed.") \
                != QMessageBox.StandardButton.Yes:
            return
        del self.profiles[self._row]
        self._row = -1
        self._fill_list()
        if self.profiles:
            self.list.setCurrentRow(0)
        self._enable()

    # ------------------------------------------------------------------ actions
    def _runner(self):
        s = self.sv.session
        if s is not None and s.runner is not None:
            return s.runner
        from aethercfd.backend.wsl.runner import WslRunner

        w = self.sv.settings.wsl
        return WslRunner(w.default_distro, allowed=w.allowed_distros)

    def _test(self) -> None:
        if not self._commit():
            return
        prof = self.profiles[self._row]
        self.report.setPlainText(f"Connecting to {prof.destination} (non-interactive)…")
        self.test_btn.setEnabled(False)

        def work():
            from aethercfd.runs.cluster.transport import check_connection

            return check_connection(prof, self._runner())

        def done(res):
            value, err = res
            self.test_btn.setEnabled(True)
            if err is not None:
                self.report.setPlainText(f"PROBLEM  {err}")
                return
            lines, _work = value
            self.report.setPlainText("\n".join(f"{'ok     ' if ln.ok else 'PROBLEM' if ln.ok is False else 'info   '}"
                                               f"  {ln.text}" for ln in lines))

        self.sv.run_bg(work, done)

    def _connect(self) -> None:
        if not self._commit():
            return
        prof = self.profiles[self._row]
        try:
            from aethercfd.runs.cluster.transport import WslSshTransport

            runner = self._runner()
            runner.open_console(WslSshTransport(prof, runner).master_console_argv())
        except Exception as exc:
            QMessageBox.warning(self, "Connect", f"Could not open the sign-in console: {exc}")
            return
        self.report.setPlainText("A console window opened: sign in there (password / one-time code), then press "
                                 "Test connection.")

    def _save(self) -> None:
        if not self._commit():
            return
        names = [p.name for p in self.profiles]
        if len(set(names)) != len(names):
            QMessageBox.warning(self, "Clusters", "Two profiles have the same name.")
            return
        self.sv.settings.clusters = list(self.profiles)
        self.sv.settings.save()
        s = self.sv.session
        if s is not None:  # the open session plans runs with its own settings object
            s.settings.clusters = list(self.profiles)
        self.accept()
