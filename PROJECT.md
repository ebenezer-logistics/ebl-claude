# Platform: the /ebl skill, installer and join service

| | |
|---|---|
| id | ebl-claude |
| type | work |
| owner | Muhammad Alif, alif@ebl.sg |
| backup person | none yet |
| status | running |
| runs where | skill on every EBL Claude PC; join service on DigitalOcean, user root, folder /root/ebl-join, process ebl-join; page at ebl.sg/claude |
| WhatsApp number | none; approval requests reach the admin through the fleet bot's outbox |
| schedule | on each PC: sync after every Claude turn and nightly 21:30; self-update nightly |
| last updated | 11/09/2026 |

## What it does
Keeps a copy of everything EBL staff build with Claude on the company server, with a one-page description per
project, so work never sits on one laptop and the next person can pick it up. Staff install it once from
ebl.sg/claude; after that it runs by itself. New people request a space with their @ebl.sg mailbox, the admin taps
Approve on WhatsApp, and their PC collects its own key.

## Who it is for
Every EBL employee who uses Claude Code. The admin (shelf owner) reads the shelf with "ebl list".

## What it connects to
- DigitalOcean droplet Ebl-Bots: shelf at /srv/ebl-shelf/<user>/, one Linux user per person, join service on 127.0.0.1:8096 behind nginx.
- Brevo (transactional email) for the six-digit mailbox codes; sender alif2@ebl.sg.
- Fleet bot manual outbox (/root/fleet-query-bot/manual-outbox.jsonl) to reach the admin on WhatsApp.
- GitHub: public repo ebenezer-logistics/ebl-claude holds the skill; the installer downloads it from there.

## Secrets it needs (names only)
- Server login per person: in that person's ~/.ebl/publish.env, issued by the join service, never in this repo.
- Brevo API key and the join service admin token: in /root/ebl-join/config.json on the server (0600).
- Deploy login for the admin's PC: ~/.ebl/deploy.env.

## How to run it
- Page and installer: `py web/deploy.py`. Join service: `py join/deploy.py --sender alif2@ebl.sg` (own pm2 process, safe to restart any time).
- Health: https://ebl.sg/api/ebl-join/health; `pm2 list` on the server shows ebl-join online.
- Owner commands in Claude: "ebl list", "ebl requests", "approve <email>", "deny <email>".

## How to set this up again from nothing
1. Create /srv/ebl-shelf with one folder per user (700, owned by the user) and _events.log (666).
2. Run join/deploy.py (writes config once, nginx locations, pm2 process) and web/deploy.py (page, installer, version.txt, QR).
3. Each PC: paste the install line from ebl.sg/claude; the owner's PC needs ROLE=owner (and SYNC=on to sync itself) in publish.env.

## How to reuse this for another customer
Not customer-specific. The pattern (public skill + private per-person key + mailbox-verified join + WhatsApp approval)
fits any tool EBL wants every seat to carry.

## Known problems and rules
- Never put a name, email, password or server address into anything a staff member sees; the repo is public.
- Never test self-update against the repository copy of the skill (it once overwrote unsaved edits).
- Release routine: bump VERSION in scripts/publish.py, both manifests and web/version.txt, add a CHANGELOG line, commit, push, run web/deploy.py; join/deploy.py only if ebl-join.py changed.
