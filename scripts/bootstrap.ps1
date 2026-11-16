<#
.SYNOPSIS
  Checks a Windows PC for what AetherCFD Studio needs and says how to get what is missing.

.DESCRIPTION
  Run it from the cloned AetherCFD folder:

      powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1

  Without switches it only looks. It never installs software, never changes WSL or Windows
  settings and never starts a WSL distro. The switches run the documented source steps in this
  folder only (uv puts everything in .venv inside the clone):

      -Sync     uv sync --locked          (Python 3.12 and the packages, into .venv)
      -Test     uv run pytest tests/unit  (the same unit tests as CI)
      -Doctor   uv run aethercfd doctor   (what works, with a fix for each problem)

  Exit code: 0 when every required part is there, 1 otherwise.
#>
[CmdletBinding()]
param(
    [switch]$Sync,
    [switch]$Test,
    [switch]$Doctor
)

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
$script:Fails = 0
$script:Warns = 0

function Report([string]$Status, [string]$Name, [string]$Detail, [string]$Fix = '') {
    $tag = @{ ok = 'ok  '; warn = 'warn'; fail = 'FAIL' }[$Status]
    Write-Host ('  [{0}] {1,-22} {2}' -f $tag, $Name, $Detail)
    if ($Fix -and $Status -ne 'ok') { Write-Host ('         {0,-22} fix: {1}' -f '', $Fix) }
    if ($Status -eq 'fail') { $script:Fails++ }
    if ($Status -eq 'warn') { $script:Warns++ }
}

function Find-Exe([string]$Name) {
    $c = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return $c.Source }
    return $null
}

function Find-InProgramFiles([string]$Pattern, [string]$Rel) {
    $roots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}, (Join-Path $env:LOCALAPPDATA 'Programs')) |
        Where-Object { $_ -and (Test-Path $_) }
    foreach ($r in $roots) {
        $hit = Get-ChildItem -Path $r -Directory -Filter $Pattern -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            ForEach-Object { Join-Path $_.FullName $Rel } |
            Where-Object { Test-Path $_ } |
            Select-Object -First 1
        if ($hit) { return $hit }
    }
    return $null
}

Write-Host "AetherCFD Studio: new PC check ($Root)"
Write-Host ''

# ------------------------------------------------------------------------------------------ Windows
Write-Host 'Windows'
$os = Get-CimInstance Win32_OperatingSystem
$build = [int]$os.BuildNumber
if ($build -ge 22000) {
    Report ok 'Windows' "$($os.Caption) (build $build)"
} elseif ($build -ge 19044) {
    Report warn 'Windows' "$($os.Caption) (build $build)" 'Windows 11 is tested; Windows 10 21H2 or newer should work'
} else {
    Report fail 'Windows' "$($os.Caption) (build $build)" 'WSL 2 needs Windows 10 21H2 or newer; Windows 11 is recommended'
}
if ([Environment]::Is64BitOperatingSystem) { Report ok '64-bit' 'yes' } else { Report fail '64-bit' 'no' 'AetherCFD needs 64-bit Windows' }

$drive = (Get-Item $Root).PSDrive
$freeGb = [math]::Round($drive.Free / 1GB, 1)
if ($freeGb -ge 15) {
    Report ok 'disk space' "$freeGb GB free on $($drive.Name):"
} else {
    Report warn 'disk space' "$freeGb GB free on $($drive.Name):" 'the .venv needs about 6 GB and a build about 8 GB more; free some space'
}
$commitGb = [math]::Round($os.FreeVirtualMemory / 1MB, 1)
if ($commitGb -ge 12) {
    Report ok 'free memory' "$commitGb GB commit memory free"
} else {
    Report warn 'free memory' "$commitGb GB commit memory free" 'only matters for packaging/build.py (needs about 12 GB): close large programs first'
}

# ------------------------------------------------------------------------------------------ tools
Write-Host ''
Write-Host 'Tools for the source code'
$git = Find-Exe 'git'
if ($git) { Report ok 'Git' ((& git --version) -join ' ') } else { Report fail 'Git' 'not found' 'winget install --id Git.Git -e   (then open a new PowerShell)' }
$uv = Find-Exe 'uv'
if ($uv) {
    Report ok 'uv' ((& uv --version) -join ' ')
} else {
    Report fail 'uv' 'not found' 'winget install --id astral-sh.uv -e   (then open a new PowerShell)'
}

$isClone = (Test-Path (Join-Path $Root 'pyproject.toml')) -and (Test-Path (Join-Path $Root '.git'))
if ($isClone) {
    $branch = ''
    if ($git) { $branch = (& git -C $Root rev-parse --abbrev-ref HEAD 2>$null) -join '' }
    Report ok 'AetherCFD source' "$Root (branch $branch)"
} else {
    Report fail 'AetherCFD source' "$Root is not a clone" 'git clone https://github.com/sidsharma2/AetherCFD.git, then run this script inside it'
}
$xterm = Join-Path $Root 'src\aethercfd\studio\resources\xterm\dist\xterm.js'
if (Test-Path $xterm) {
    Report ok 'terminal files' 'xterm.js present'
} else {
    Report fail 'terminal files' 'xterm.js missing' 'git checkout -- src/aethercfd/studio/resources/xterm'
}
if ($uv) {
    $py = (& uv python find 3.12 2>$null) -join ''
    if ($LASTEXITCODE -eq 0 -and $py) {
        Report ok 'Python 3.12' $py
    } else {
        Report warn 'Python 3.12' 'not installed yet' 'uv sync downloads it (run this script with -Sync)'
    }
}
$venvPy = Join-Path $Root '.venv\Scripts\python.exe'
if (Test-Path $venvPy) {
    Report ok '.venv' $venvPy
} else {
    Report warn '.venv' 'not created yet' 'uv sync --locked   (or run this script with -Sync)'
}

# ------------------------------------------------------------------------------------------ WSL
Write-Host ''
Write-Host 'WSL and OpenFOAM (only listed here: no distro is started)'
$wsl = Find-Exe 'wsl.exe'
if (-not $wsl) {
    Report fail 'WSL' 'wsl.exe not found' 'in an admin PowerShell: wsl --install --no-distribution, then restart Windows'
} else {
    $old = $env:WSL_UTF8
    $env:WSL_UTF8 = '1'  # this process only: wsl.exe prints UTF-8 instead of UTF-16
    $lines = & wsl.exe --list --verbose 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE
    $env:WSL_UTF8 = $old
    $rows = @($lines | Select-Object -Skip 1 | ForEach-Object { $_.Trim() } | Where-Object { $_ -match '\S+\s+\S+\s+\d$' })
    if ($code -ne 0 -and $rows.Count -eq 0) {
        Report fail 'WSL' 'installed but has no distro, or is not enabled' 'wsl --install -d Ubuntu-22.04   (needs WSL enabled: wsl --install --no-distribution as admin, restart)'
    } else {
        $usable = @($rows | Where-Object { $_ -notmatch '^\*?\s*(docker-desktop|docker-desktop-data|rancher-desktop|podman-machine-default|Agora)\s' })
        Report ok 'WSL' ("{0} distro(s)" -f $rows.Count)
        foreach ($r in $rows) { Write-Host ('         {0}' -f $r) }
        if ($usable.Count -eq 0) {
            Report fail 'Linux distro' 'none AetherCFD can use' 'wsl --install -d Ubuntu-22.04'
        } else {
            Report ok 'Linux distro' 'present; OpenFOAM inside it is checked by the setup wizard or: uv run aethercfd doctor --probe'
        }
    }
}

# ------------------------------------------------------------------------------------------ optional
Write-Host ''
Write-Host 'Optional'
$smi = Find-Exe 'nvidia-smi'
if ($smi) {
    $gpu = (& nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>$null) -join '; '
    Report ok 'NVIDIA GPU' $gpu
} else {
    Report warn 'NVIDIA GPU' 'no NVIDIA driver found' 'optional: GPU features then run on the CPU'
}
$pv = Find-InProgramFiles 'ParaView*' 'bin\paraview.exe'
if ($pv) { Report ok 'ParaView' $pv } else { Report warn 'ParaView' 'not found' 'optional: paraview.org/download' }
$fc = Find-InProgramFiles 'FreeCAD*' 'bin\FreeCADCmd.exe'
if ($fc) { Report ok 'FreeCAD' $fc } else { Report warn 'FreeCAD' 'not found' 'optional: freecad.org/downloads' }
$mlEnv = @((Join-Path $Root '.ml-env\Scripts\python.exe'), (Join-Path $env:LOCALAPPDATA 'AetherCFD\ml-env\Scripts\python.exe')) |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if ($mlEnv) { Report ok 'PyTorch env (PINNs)' $mlEnv } else { Report warn 'PyTorch env (PINNs)' 'not found' 'optional: uv run python scripts/setup_ml_env.py' }
$claude = Find-Exe 'claude'
if ($claude) { Report ok 'Claude Code' $claude } else { Report warn 'Claude Code' 'not found' 'optional, for the copilot: see claude.com/code' }
$codex = Find-Exe 'codex'
if ($codex) { Report ok 'Codex CLI' $codex } else { Report warn 'Codex CLI' 'not found' 'optional, for the copilot: npm install -g @openai/codex (needs Node.js)' }

Write-Host ''
Write-Host ("{0} problem(s), {1} warning(s)" -f $script:Fails, $script:Warns)

# ------------------------------------------------------------------------------------------ steps asked for
function Step([string]$Title, [scriptblock]$Block) {
    Write-Host ''
    Write-Host "== $Title"
    & $Block
    if ($LASTEXITCODE -ne 0) {
        Write-Host "== $Title failed (exit $LASTEXITCODE)"
        exit $LASTEXITCODE
    }
}

if (($Sync -or $Test -or $Doctor) -and -not ($uv -and $isClone)) {
    Write-Host 'Install uv and run this script inside a clone first.'
    exit 1
}
Push-Location $Root
try {
    if ($Sync) { Step 'uv sync --locked' { uv sync --locked } }
    if ($Test) { Step 'unit tests' { uv run pytest tests/unit -m 'not wsl and not wsl_slow and not corpus and not gl' -q } }
    if ($Doctor) { Step 'aethercfd doctor' { uv run aethercfd doctor } }
} finally {
    Pop-Location
}

if ($script:Fails -gt 0) { exit 1 }
exit 0
