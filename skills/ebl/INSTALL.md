# Installing /ebl on a PC

Two minutes, once per computer. Windows with Claude Code installed.

## 1. Paste one line
Open PowerShell (Start, type PowerShell, Enter) and paste:
```
irm https://ebl.sg/claude/install.ps1 | iex
```
It downloads the `ebl` skill from EBL's GitHub into `%USERPROFILE%\.claude\skills\ebl\` and installs the one
Python library it needs. Run the same line again any time to update.

## 2. Answer three questions
In the same window: your full name, your @ebl.sg email, and the six-digit code that arrives in that mailbox.
Then close the window.

## 3. Wait
EBL is asked to approve your space. When approved, your PC finishes setting itself up on its own within ten
minutes. Nothing to type, nothing to install, nobody hands you a file.

## Then
Work in Claude Code as usual. Whatever you build for EBL is kept on the company shelf automatically. Personal
projects send only a one-page note. Chats are never sent. `ebl sync off` pauses it on this PC, `ebl sync on` resumes.

## If something is missing
- "py is not recognised": install Python 3 from https://www.python.org/downloads/ (tick "Add to PATH"), then run
  the install line again.
- "Could not send the code": check the email is an @ebl.sg address, or try again in a minute.
- Still waiting after a day: ask the EBL admin whether the request reached them.
