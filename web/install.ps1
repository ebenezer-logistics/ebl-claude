# EBL: install or update the /ebl skill for Claude Code on this PC.
# Run:  irm https://ebl.sg/claude/install.ps1 | iex
# Safe to run again. Never touches your settings file (~\.ebl\publish.env).
$ErrorActionPreference = "Stop"
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}

$zipUrl  = "https://github.com/ebenezer-logistics/ebl-claude/archive/refs/heads/main.zip"
$skills  = Join-Path $env:USERPROFILE ".claude\skills"
$target  = Join-Path $skills "ebl"
$eblDir  = Join-Path $env:USERPROFILE ".ebl"
$tmp     = Join-Path $env:TEMP ("ebl-claude-" + [guid]::NewGuid().ToString("N"))

Write-Host ""
Write-Host "EBL: installing the /ebl skill for Claude Code" -ForegroundColor Cyan

New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$zip = Join-Path $tmp "ebl-claude.zip"
Invoke-WebRequest -Uri $zipUrl -OutFile $zip -UseBasicParsing
Expand-Archive -Path $zip -DestinationPath $tmp -Force
$src = Join-Path $tmp "ebl-claude-main\skills\ebl"
if (-not (Test-Path (Join-Path $src "SKILL.md"))) { throw "Download looked wrong (no SKILL.md). Try again or tell the EBL admin." }

New-Item -ItemType Directory -Force -Path $skills | Out-Null
if (Test-Path $target) { Remove-Item -Recurse -Force $target }
Copy-Item -Recurse -Force $src $target
# The old name, if this PC had the first version.
$old = Join-Path $skills "ebl-publish"
if (Test-Path $old) { Remove-Item -Recurse -Force $old; Write-Host "  removed the old ebl-publish skill (replaced by /ebl)" }
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue

New-Item -ItemType Directory -Force -Path $eblDir | Out-Null
$ver = (Select-String -Path (Join-Path $target "scripts\publish.py") -Pattern '^VERSION = "([^"]+)"').Matches[0].Groups[1].Value
Write-Host "  skill installed: $target  (version $ver)" -ForegroundColor Green

# Python + paramiko. From here on, programs may print warnings to their error stream; Windows PowerShell 5.1
# would treat that as fatal under "Stop", so relax it. Nothing below can leave the PC half-installed.
$ErrorActionPreference = "Continue"
$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
if ($py) {
  # Quiet check: prints nothing, exit code 1 if the library is missing.
  & $py.Source -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('paramiko') else 1)"
  if ($LASTEXITCODE -ne 0) {
    Write-Host "  installing the one Python library it needs (paramiko)..."
    & $py.Source -m pip install --quiet --disable-pip-version-check paramiko
    & $py.Source -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('paramiko') else 1)"
    if ($LASTEXITCODE -ne 0) { Write-Host "  could not install paramiko. In this window run:  py -m pip install paramiko   then run the install line again." -ForegroundColor Yellow; return }
  }
  Write-Host "  python: ok" -ForegroundColor Green
  # Wire the automatic sync: a Claude Code hook after each turn + a nightly task. Safe to repeat.
  & $py.Source (Join-Path $target "scripts\autosync.py") install
} else {
  Write-Host "  python: NOT FOUND. Install Python 3 from https://www.python.org/downloads/ (tick 'Add to PATH'), then run this line again." -ForegroundColor Yellow
}

$envFile  = Join-Path $eblDir "publish.env"
$joinFile = Join-Path $eblDir "join.json"
$auto     = Join-Path $target "scripts\autosync.py"
if (Test-Path $envFile) {
  Write-Host "  settings: found ($envFile)" -ForegroundColor Green
  Write-Host ""
  Write-Host "Done. Nothing else to do." -ForegroundColor Cyan
} elseif (Test-Path $joinFile) {
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
  Write-Host "  Install Python first (see above), then run this line again." -ForegroundColor Yellow
}
Write-Host ""
