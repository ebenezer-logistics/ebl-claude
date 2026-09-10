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
if (-not (Test-Path (Join-Path $src "SKILL.md"))) { throw "Download looked wrong (no SKILL.md). Try again or tell Alif." }

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

# Python + paramiko
$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
if ($py) {
  & $py.Source -c "import paramiko" 2>$null
  if ($LASTEXITCODE -ne 0) {
    Write-Host "  installing the one Python library it needs (paramiko)..."
    & $py.Source -m pip install --quiet --disable-pip-warnings paramiko
  }
  Write-Host "  python: ok" -ForegroundColor Green
  # Wire the automatic sync: a Claude Code hook after each turn + a nightly task. Safe to repeat.
  & $py.Source (Join-Path $target "scripts\autosync.py") install
} else {
  Write-Host "  python: NOT FOUND. Install Python 3 from https://www.python.org/downloads/ (tick 'Add to PATH'), then run this line again." -ForegroundColor Yellow
}

$envFile = Join-Path $eblDir "publish.env"
if (Test-Path $envFile) {
  Write-Host "  settings: found ($envFile)" -ForegroundColor Green
  Write-Host ""
  Write-Host "Done. Open Claude Code and say: ebl check" -ForegroundColor Cyan
} else {
  Write-Host "  settings: not yet. Ask Alif for your EBL settings file and save it as:" -ForegroundColor Yellow
  Write-Host "           $envFile"
  Write-Host ""
  Write-Host "Then open Claude Code and say: ebl check" -ForegroundColor Cyan
}
Write-Host ""
