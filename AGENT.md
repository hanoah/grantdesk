# AGENT.md — setting up GrantDesk for someone

You are an AI agent on a computer that has this folder. Your job: get GrantDesk running and get the person's existing grant list into it. Follow these steps in order. Read `README.md` for how the app works.

## 1. Start the app

```bash
cd <this folder>
python3 app.py --no-browser
```

Confirm it prints `GrantDesk running at http://localhost:8765`. If `python3` is not found, try `python`. If the port is busy, add `--port 8766`. Leave it running (background it) and tell the person to open the URL. Do not install anything — the app uses only the Python standard library.

If SQLite reports `disk I/O error`, the folder is on a filesystem that cannot take locks (some network/synced mounts). Copy the folder to a local path such as the user's home directory and run from there.

## 2. Ask for their spreadsheet

Ask the person for the spreadsheet or list they currently track grants in (Excel, CSV, Google Sheet, Numbers, or even a pasted table). If it's a Google Sheet, ask them to File → Download → Microsoft Excel (.xlsx), or share it so you can read it.

If they have no list yet, skip to step 5 and show them **Add new prospect**.

## 3. Fill the template

Produce the blank template:

```bash
python3 app.py --template grants_template.xlsx
```

Open the person's file and map their columns into the template's **Grants** sheet. Column guide (also in the template's second sheet):

| Template column | Put here |
|---|---|
| Grant Name | required — short title |
| Organisation | applicant / partner |
| Summary | 1–2 sentences |
| Stage | one of: Inbound, Prospect, First Meeting, Due Diligence, Second Meeting, Deep Dive, Paperwork, Awarded, Declined — default Inbound |
| Reach (1-5), Impact (1-5), Effort (1-5) | integers; leave blank if unknown (defaults to 3) |
| Confidence (%) | 0–100; leave blank if unknown (defaults to 50) |
| Amount | number only |
| Deadline (YYYY-MM-DD) | ISO date |
| Primary Contact Name / Email | main person |
| Other Contacts | `Name <email>; Name <email>` |
| Last Interaction (YYYY-MM-DD) | date of last email/meeting; defaults to today |
| Ball In Court | `Ours` or `Theirs` |
| Starred (Y/N) | Y to favourite |
| Notes | anything else |

Rules of thumb when mapping: a "status" column usually maps to Stage (match loosely — "first call" → First Meeting, "approved" → Awarded, "rejected"/"no" → Declined, anything unknown → Inbound). Money columns with currency symbols: strip symbols. If they have a free-text "next step" or "waiting on" column, use it to set Ball In Court (if it says they are waiting on the applicant → Theirs; otherwise Ours) and put the text in Notes. Do not invent scores; leave blank unless the sheet has an obvious priority column you can scale to 1–5.

Keep the header row exactly as in the template so the import maps automatically. Save as `.xlsx` (or `.csv`).

You can also skip the spreadsheet step and import directly with the API:

```bash
curl -s -F "file=@grants_filled.xlsx" http://localhost:8765/api/import/preview > preview.json
# inspect preview.json: "mapping" shows detected columns, "rows" the data
python3 - <<'EOF'
import json, urllib.request
p = json.load(open("preview.json"))
body = json.dumps({"rows": p["rows"], "mapping": p["mapping"], "mode": "append"}).encode()
req = urllib.request.Request("http://localhost:8765/api/import/commit", data=body, headers={"Content-Type": "application/json"})
print(urllib.request.urlopen(req).read().decode())
EOF
```

`mode` is `append` (default) or `replace` (deletes existing grants first — confirm with the person before using it).

## 4. Import and check

Either use the **Import** page in the browser (download template → upload → check mapping → Import), or the API above. Then open the Pipeline page and sanity-check with the person: right number of grants, stages look right, contacts attached.

## 5. Show them the three things that matter

1. **Triage**: the ⚡ button on the Pipeline page steps through everything in Inbound. Enter keeps it (→ Prospect), D declines, S skips. Sliders set the score.
2. **The tennis court**: red = we owe a reply; green = waiting on them. It flips automatically when they log an email, meeting or call on a grant's page.
3. **Add new prospect**: paste an email, it fills itself in.

## 6. Optional: AI

If the person wants smarter suggestions, they can connect a model provider in ⚙︎ Settings themselves (OpenAI, Anthropic, local Ollama, or any OpenAI-compatible endpoint). Never ask them to paste an API key to you in chat, never type it into any file, and never log it. Everything works without it.

## Other useful endpoints

- `GET /api/grants` — all grants as JSON
- `GET /api/grants/<id>` — one grant with contacts, messages and threads
- `POST /api/seed` — load demo data (replaces current grants)
- `GET /api/export.xlsx`, `GET /api/export.json` — backups
- `POST /api/grants/<id>/messages` — log an interaction: `{"kind":"email|meeting|call|note","direction":"in|out","subject":"…","body":"…","sent_at":"YYYY-MM-DD HH:MM:SS"}`. `in` = ball comes to us, `out` = ball goes to them.
