# Installing /ebl on a PC

Two minutes. Windows with Claude Code installed.

## 1. Install the skill (one line)
Open PowerShell (Start, type PowerShell, Enter) and paste:
```
irm https://ebl.sg/claude/install.ps1 | iex
```
It downloads the `ebl` skill from EBL's GitHub into `%USERPROFILE%\.claude\skills\ebl\` and installs the one
Python library it needs. Run the same line again any time to update; it keeps your settings.

You can also paste that line into Claude Code and say "run this in PowerShell". Same result.

## 2. Save your settings file
Alif gives you a five-line file when you join. Save it as:
```
%USERPROFILE%\.ebl\publish.env
```
(The installer creates the `.ebl` folder.) The file holds your own server login. Never put it in a chat or in a
project folder. Losing it is fine: Alif issues a new one.

## 3. Check
Open Claude Code anywhere and say **ebl check**. Four lines, all ok, then READY.

## Using it
Open Claude Code in any project folder and say **publish to EBL**. Answer the few questions. Say yes when it shows
you the PROJECT.md. Done. Say **ebl status** to check on it later. Say **publish to EBL** again whenever the project
changes.

## If something is missing
- "py is not recognised": install Python 3 from https://www.python.org/downloads/ (tick "Add to PATH"), then run
  the install line again.
- "paramiko is missing": in PowerShell, `py -m pip install paramiko`.
- "settings: MISSING": ask Alif for your file.
- "shelf: MISSING": ask Alif to create your shelf on the server.
