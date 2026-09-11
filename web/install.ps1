# EBL: install or update the /ebl skill for Claude Code on this PC.
# Run:  irm https://ebl.sg/claude/install.ps1 | iex
# Safe to run again. Never touches your settings file (~\.ebl\publish.env).
$ErrorActionPreference = "Stop"
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}

$zipUrl  = "https://github.com/ebenezer-logistics/ebl-claude/archive/refs/heads/main.zip"
$skills  = Join-Path $env:USERPROFILE ".claude\skills"
$target  = Join-Path $skills "ebl"
$eblDir  = Join-Path $env:USERPROFILE ".ebl"
# Work folder for the download lives under the user's own .ebl folder, never in %TEMP%: on accounts with a space
# in the name (e.g. "Xin Yi") Windows hands out a short path like C:\Users\XINYI~1\..., and PowerShell reads the
# squiggle as "home folder", so Remove-Item fails. -LiteralPath everywhere for the same reason.
$tmp     = Join-Path $eblDir ("tmp-install-" + [guid]::NewGuid().ToString("N"))

Write-Host ""
Write-Host "EBL: installing the /ebl skill for Claude Code" -ForegroundColor Cyan

New-Item -ItemType Directory -Force -Path $eblDir | Out-Null
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$zip = Join-Path $tmp "ebl-claude.zip"
Invoke-WebRequest -Uri $zipUrl -OutFile $zip -UseBasicParsing
Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
$src = Join-Path $tmp "ebl-claude-main\skills\ebl"
if (-not (Test-Path -LiteralPath (Join-Path $src "SKILL.md"))) { throw "Download looked wrong (no SKILL.md). Try again or tell the EBL admin." }

New-Item -ItemType Directory -Force -Path $skills | Out-Null
if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
Copy-Item -LiteralPath $src -Destination $target -Recurse -Force
# The old name, if this PC had the first version.
$old = Join-Path $skills "ebl-publish"
if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force; Write-Host "  removed the old ebl-publish skill (replaced by /ebl)" }
try { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction Stop } catch { }
# Leftovers from any earlier interrupted run.
try { Get-ChildItem -LiteralPath $eblDir -Directory -Filter "tmp-install-*" -ErrorAction Stop | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue } catch { }
$ver = (Select-String -Path (Join-Path $target "scripts\publish.py") -Pattern '^VERSION = "([^"]+)"').Matches[0].Groups[1].Value
Write-Host "  skill installed: $target  (version $ver)" -ForegroundColor Green

# Python + paramiko. From here on, programs may print warnings to their error stream; Windows PowerShell 5.1
# would treat that as fatal under "Stop", so relax it. Nothing below can leave the PC half-installed.
$ErrorActionPreference = "Continue"

# Windows ships a fake "python.exe" that only says "install from the Microsoft Store". Only accept a Python that
# actually answers. If none does, install one quietly from python.org (per user, no admin, about a minute).
function Get-RealPython {
  foreach ($n in @("py", "python3", "python")) {
    $c = Get-Command $n -ErrorAction SilentlyContinue
    if ($c) {
      $out = & $c.Source -c "import sys;print(sys.version_info[0])" 2>&1
      if ($LASTEXITCODE -eq 0 -and "$out" -match "^3") { return $c }
    }
  }
  return $null
}
$py = Get-RealPython
if (-not $py) {
  Write-Host "  python: not on this PC yet. Installing Python 3 for you (about a minute, nothing to click)..." -ForegroundColor Yellow
  $exe = Join-Path $eblDir "python-installer.exe"
  try {
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.12.6/python-3.12.6-amd64.exe" -OutFile $exe -UseBasicParsing
    $p = Start-Process -FilePath $exe -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_launcher=1", "Include_test=0", "Include_doc=0" -Wait -PassThru
    if ($p.ExitCode -ne 0) { Write-Host "  Python installer returned code $($p.ExitCode)." -ForegroundColor Yellow }
  } catch { Write-Host "  could not download Python ($($_.Exception.Message))." -ForegroundColor Yellow }
  Remove-Item -LiteralPath $exe -Force -ErrorAction SilentlyContinue
  # Pick up the new PATH in this window, then look again.
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
  $launcher = Join-Path $env:LOCALAPPDATA "Programs\Python\Launcher"
  if (Test-Path -LiteralPath (Join-Path $launcher "py.exe")) { $env:Path = "$launcher;$env:Path" }
  $py = Get-RealPython
  if ($py) { Write-Host "  python: installed" -ForegroundColor Green }
}
if ($py) {
  # Quiet check: prints nothing, exit code 1 if the library is missing.
  & $py.Source -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('paramiko') else 1)"
  if ($LASTEXITCODE -ne 0) {
    Write-Host "  installing the one Python library it needs (paramiko)..."
    & $py.Source -m pip install --quiet --disable-pip-version-check paramiko
    & $py.Source -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('paramiko') else 1)"
    if ($LASTEXITCODE -ne 0) { Write-Host "  could not install the library. Close this window, open PowerShell again, and run the install line once more." -ForegroundColor Yellow; return }
  }
  Write-Host "  python: ok" -ForegroundColor Green
  # Wire the automatic sync: a Claude Code hook after each turn + a nightly task. Safe to repeat.
  & $py.Source (Join-Path $target "scripts\autosync.py") install
} else {
  Write-Host "  python: could not be installed automatically. Install Python 3 from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), close this window, open PowerShell again and run the install line once more." -ForegroundColor Yellow
}

$envFile  = Join-Path $eblDir "publish.env"
$joinFile = Join-Path $eblDir "join.json"
$auto     = Join-Path $target "scripts\autosync.py"
if (Test-Path -LiteralPath $envFile) {
  Write-Host "  settings: found ($envFile)" -ForegroundColor Green
  Write-Host ""
  Write-Host "Done. Nothing else to do." -ForegroundColor Cyan
} elseif (Test-Path -LiteralPath $joinFile) {
  Write-Host "  a request is already waiting for EBL's approval; checking..." -ForegroundColor Yellow
  if ($py) { & $py.Source $auto claim }
} elseif ($py) {
  # First time on this PC: ask for a space. Name, @ebl.sg email, the code from that mailbox. Then nothing, ever again.
  Write-Host ""
  Write-Host "Let's request your EBL space. Three answers, once." -ForegroundColor Cyan
  $interactive = -not [Console]::IsInputRedirected
  $name = ""; $email = ""
  if ($interactive) {
    try { $name = Read-Host "  Your full name"; $email = Read-Host "  Your @ebl.sg email" } catch { $name = "" }
  }
  if ($name -and $email) {
    & $py.Source $auto join --name "$name" --email "$email"
    if ($LASTEXITCODE -eq 0) {
      $code = Read-Host "  The six-digit code from that mailbox"
      & $py.Source $auto join-code "$code"
    }
  } else {
    # Run from inside Claude Code (no keyboard): hand the three questions to Claude.
    Write-Host ""
    Write-Host "CLAUDE: the EBL skill is installed. Now finish the join for this user, one question at a time:" -ForegroundColor Cyan
    Write-Host "  1. Ask their full name, then their @ebl.sg email."
    Write-Host "  2. Run:  py `"$auto`" join --name `"<name>`" --email <email>"
    Write-Host "  3. Ask for the six-digit code from that mailbox, then run:  py `"$auto`" join-code <code>"
    Write-Host "  4. Tell them: EBL has been asked; this PC finishes by itself; nothing more to do. Never name the admin."
  }
} else {
  Write-Host "  Python first (see above), then run the install line again." -ForegroundColor Yellow
}
Write-Host ""
