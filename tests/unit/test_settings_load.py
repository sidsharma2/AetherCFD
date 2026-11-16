"""A hand-edited settings.toml that is broken never stops the app, and the user's file is kept."""

from __future__ import annotations

from aethercfd.core.settings import Settings


def _write(tmp_path, monkeypatch, text: str):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path))
    (tmp_path / "settings.toml").write_text(text, encoding="utf-8")


def test_a_toml_syntax_error_gives_the_defaults_and_keeps_a_copy(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, 'theme = "dark"\n[wsl\ndefault_distro = "Ubuntu"\n')
    s = Settings.load()
    assert s.theme == "system" and s.wsl.default_distro == ""
    assert "could not read settings.toml" in s.load_problem and "copied to" in s.load_problem
    (copy,) = tmp_path.glob("settings.toml.broken-*")
    assert "[wsl\n" in copy.read_text(encoding="utf-8")


def test_a_bad_value_drops_only_that_value(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, 'theme = "dark"\nguards = 5\nrecent_cases = ["D:/a"]\n'
                                  '[wsl]\ndefault_distro = 1\nallowed_distros = ["Ubuntu"]\n'
                                  '[[clusters]]\nname = "ok"\nhost = "h"\nuser = "u"\n'
                                  '[[clusters]]\nname = "no host"\n')
    s = Settings.load()
    assert s.theme == "dark" and s.recent_cases == ["D:/a"] and s.wsl.allowed_distros == ["Ubuntu"]
    assert s.wsl.default_distro == "" and s.guards.log_hard_cap_gb == 10.0
    assert [c.name for c in s.clusters] == ["ok"]
    assert "wsl.default_distro" in s.load_problem and "guards" in s.load_problem
    assert list(tmp_path.glob("settings.toml.broken-*"))


def test_a_good_file_has_no_problem_and_saves_round_trip(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, 'theme = "light"\n')
    s = Settings.load()
    assert s.load_problem is None and s.theme == "light"
    s.save()
    assert Settings.load().theme == "light" and not list(tmp_path.glob("settings.toml.broken-*"))
    assert "load_problem" not in (tmp_path / "settings.toml").read_text(encoding="utf-8")


def test_a_file_that_is_not_utf8_gives_the_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path))
    (tmp_path / "settings.toml").write_bytes(b'theme = "\xff\xfe"\n')
    s = Settings.load()
    assert s.theme == "system" and s.load_problem
