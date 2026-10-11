@echo off
rem Installs AetherCFD Studio for this Windows user (no admin, no Python needed).
rem Copies the AetherCFD folder next to this file to %LOCALAPPDATA%\Programs\AetherCFD and adds
rem "AetherCFD Studio" shortcuts to the Start menu and the Desktop. The previous install is kept
rem as AetherCFD.previous until the next install.
setlocal
set "SRC=%~dp0AetherCFD"
set "DEST=%LOCALAPPDATA%\Programs\AetherCFD"

if not exist "%SRC%\AetherCFD.exe" (
  echo The AetherCFD folder was not found next to this file. Unzip the whole release first.
  pause
  exit /b 1
)
tasklist /FI "IMAGENAME eq AetherCFD.exe" 2>nul | find /I "AetherCFD.exe" >nul
if not errorlevel 1 (
  echo AetherCFD is running. Close it, then run this again.
  pause
  exit /b 2
)

echo Installing AetherCFD Studio to %DEST% ...
if exist "%DEST%.new" rmdir /S /Q "%DEST%.new"
robocopy "%SRC%" "%DEST%.new" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
  echo Copying failed. Is there enough free disk space?
  pause
  exit /b 3
)
if exist "%DEST%" (
  if exist "%DEST%.previous" rmdir /S /Q "%DEST%.previous"
  move "%DEST%" "%DEST%.previous" >nul
)
move "%DEST%.new" "%DEST%" >nul

powershell -NoProfile -Command "$w = New-Object -ComObject WScript.Shell; foreach ($f in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) { $s = $w.CreateShortcut((Join-Path $f 'AetherCFD Studio.lnk')); $s.TargetPath = $env:DEST + '\AetherCFD.exe'; $s.WorkingDirectory = $env:DEST; $s.IconLocation = $env:DEST + '\AetherCFD.exe,0'; $s.Description = 'AetherCFD Studio - OpenFOAM workbench'; $s.Save() }"

echo.
echo Done. Start "AetherCFD Studio" from the Start menu or the Desktop.
echo On the first start a setup wizard checks WSL, OpenFOAM and the copilot sign-in (see INSTALL.md).
pause
