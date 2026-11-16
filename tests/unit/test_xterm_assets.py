"""The Terminal panel's xterm.js files are committed: a clone, a worktree and a build must all have them."""

from __future__ import annotations

import json
import shutil
import subprocess

from aethercfd.core.runtime import XTERM_DIR, terminal_asset_problems, xterm_manifest


def test_vendored_xterm_files_are_present_and_unchanged():
    assert terminal_asset_problems(verify_hashes=True) == []


def test_manifest_matches_package_json():
    pkg = json.loads((XTERM_DIR / "package.json").read_text(encoding="utf-8"))["dependencies"]
    man = xterm_manifest()
    assert man["packages"] == pkg
    assert {"xterm.js", "xterm.css", "addon-fit.js", "addon-search.js", "addon-unicode11.js",
            "LICENSE-xterm"} <= set(man["files"])
    for meta in man["files"].values():  # every file comes from a pinned package
        assert any(meta["from"].startswith(p + "/") for p in pkg)


def test_terminal_page_loads_only_vendored_files():
    html = (XTERM_DIR / "terminal.html").read_text(encoding="utf-8")
    for name in ("xterm.css", "xterm.js", "addon-fit.js"):
        assert f"dist/{name}" in html


def test_missing_or_changed_files_are_reported(tmp_path):
    copy = tmp_path / "xterm"
    shutil.copytree(XTERM_DIR, copy, ignore=shutil.ignore_patterns("node_modules"))
    assert terminal_asset_problems(copy, verify_hashes=True) == []
    (copy / "dist" / "xterm.js").unlink()
    (copy / "dist" / "xterm.css").write_text("changed", encoding="utf-8")
    problems = terminal_asset_problems(copy, verify_hashes=True)
    assert any("missing" in p and "xterm.js" in p for p in problems)
    assert any("changed" in p and "xterm.css" in p for p in problems)
    assert len(terminal_asset_problems(copy)) == 1  # without hashes only the missing file counts
    (copy / "vendor.json").unlink()
    assert "vendor.json" in terminal_asset_problems(copy)[0]


def test_vendored_files_are_tracked_by_git():
    """A blanket `dist/` ignore rule once kept these out of every clone ("Terminal is not defined")."""
    git = shutil.which("git")
    root = XTERM_DIR.parents[4]
    if git is None or not (root / ".git").exists():
        return  # not a git checkout (e.g. an unpacked source archive)
    names = [f"src/aethercfd/studio/resources/xterm/dist/{n}" for n in xterm_manifest()["files"]]
    r = subprocess.run([git, "check-ignore", "--no-index", *names], cwd=root, capture_output=True, text=True)
    assert r.stdout.strip() == "", f"ignored by .gitignore: {r.stdout}"

