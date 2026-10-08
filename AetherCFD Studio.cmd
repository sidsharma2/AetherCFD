@echo off
rem AetherCFD Studio launcher (no console window). Pass a case folder to open it.
start "" "%~dp0.venv\Scripts\pythonw.exe" -m aethercfd.studio %*
