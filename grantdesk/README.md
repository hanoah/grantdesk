# GrantDesk

A small, zero-dependency grant pipeline tracker. One Python file, one SQLite database, no installs.

## Run it

```bash
python3 app.py
```

That's it. It opens http://localhost:8765 in your browser. Needs Python 3.8+ (already on every Mac and most Linux machines; on Windows install from python.org and tick "Add to PATH").

Useful flags:

```bash
python3 app.py --seed          # load 14 demo grants first (replaces anything already there)
python3 app.py --port 9000     # use a different port
python3 app.py --template grants_template.xlsx   # just write the blank Excel template and exit
```

Your data lives in `grantdesk.db` next to `app.py`. Copy that file to back up or move the whole thing to another machine.

## What's in it

**Pipeline** (home) — every grant in one table. Star the ones you care about; they float to the top. The tennis court shows whose move it is: **red = ball in our court**, **green = ball in theirs**. "Days idle" counts since the last logged interaction. Click a score to triage it on the spot, or hit **⚡ Triage inbound** to step through everything new with keyboard shortcuts (Enter = keep, D = decline, S = skip).

**Scoring** — RICE adapted for grants: `Reach × Impact × Confidence ÷ Effort`.
Reach = how many people it touches · Impact = how much it changes things for them · Confidence = how sure we are it will deliver · Effort = how far we are from realising it.

**Board** — the same grants as a kanban. Drag cards between stages: Inbound → Prospect → First Meeting → Due Diligence → Second Meeting → Deep Dive → Paperwork → Awarded / Declined.

**Import** — download the Excel template, fill it in (or paste from the spreadsheet you already have), upload it. Columns are matched automatically and you can fix the mapping before committing. Also takes CSV. Google Sheets: File → Download → .xlsx.

**Grant detail** — click any grant. People linked to it, the full activity log (emails grouped into threads, plus meetings, calls and notes), a proposed next step, and a reply box.

**Add new prospect** — paste an email or any text; names, emails, org and amount are pulled out for you.

## Whose court is it?

The court flips automatically from the activity log. An inbound email or a meeting/call where *we* owe the next action puts the ball in our court; a reply we send or a meeting where *they* owe the next step puts it in theirs. Notes don't change it. You can also override it by hand on the grant page.

## AI

Without any setup, "Proposed next step" and thread summaries use built-in rules (stage, days idle, whose court, score). In **⚙︎ Settings** you can connect any model provider — OpenAI, Anthropic, a local Ollama, or any OpenAI-compatible endpoint — and those switch to a real model, and pasted prospects are parsed more intelligently. The key is stored only in `grantdesk.db` on your machine and sent only to the endpoint you configure. Everything works without it.

## Prototype-only bits (marked "coming soon" in the UI)

- Sending email from the reply box — it saves the message to the thread as *queued* and flips the court, but nothing is actually sent. "Log as already sent" is for when you sent it from your mail client.
- Forwarding emails into GrantDesk from your inbox.
- Live Google Sheets sync.

## Files

```
app.py            server, API, database, xlsx reader/writer, seed data  (stdlib only)
static/           index.html, app.js, style.css — the UI, no build step
grantdesk.db      created on first run; your data
AGENT.md          instructions for an AI agent setting this up for someone
```
