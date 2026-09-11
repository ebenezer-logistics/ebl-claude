# ebl-claude

The `/ebl` skill for Claude Code, used by everyone at Ebenezer Logistics who builds with Claude.

It does one job: send a project's plain-English `PROJECT.md` (and, for work projects, the code without secrets)
to EBL's shelf on the company server, so the company can see, understand, recover and reuse what was built.
Chats stay private. Nothing here contains a secret or a server address; each person's own settings file,
handed over privately, is what gives the skill access.

## Install (Windows, two minutes)

Open PowerShell and paste:

```
irm https://ebl.sg/claude/install.ps1 | iex
```

Full instructions: https://ebl.sg/claude or [skills/ebl/INSTALL.md](skills/ebl/INSTALL.md).

Claude Code's plugin route also works:

```
claude plugin marketplace add ebenezer-logistics/ebl-claude
claude plugin install ebl@ebl
```

## Layout

- `skills/ebl/` the skill: `SKILL.md`, `templates/PROJECT.md`, `scripts/publish.py`, `INSTALL.md`
- `web/` the onboarding page and installer served at ebl.sg/claude
- `.claude-plugin/` plugin and marketplace manifests

## What changed

See [CHANGELOG.md](CHANGELOG.md), one line per version.

## Updating

Change the skill, bump `VERSION` in `scripts/publish.py`, `version` in both manifests and `web/version.txt`,
then run `web/deploy.py` to push the page. Users re-run the install line, or the skill tells them to when it
notices a newer version.
