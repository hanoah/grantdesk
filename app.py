#!/usr/bin/env python3
"""
GrantDesk — zero-dependency grant pipeline tracker.

Run:   python3 app.py            (opens http://localhost:8765)
       python3 app.py --seed     (loads demo data first)
       python3 app.py --port 9000

Only the Python standard library is used. Data lives in grantdesk.db (SQLite)
next to this file. The .xlsx template is generated on the fly (no Excel libs).
"""
import argparse
import csv
import io
import json
import os
import re
import sqlite3
import sys
import threading
import urllib.request
import urllib.error
import webbrowser
import zipfile
from datetime import datetime, date, timedelta
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "grantdesk.db")
STATIC = os.path.join(HERE, "static")

STAGES = ["Inbound", "Prospect", "First Meeting", "Due Diligence",
          "Second Meeting", "Deep Dive", "Paperwork", "Awarded", "Declined"]
CLOSED = {"Awarded", "Declined"}

# Template columns: (header, db field, help text)
TEMPLATE_COLUMNS = [
    ("Grant Name", "name", "Required. Short name of the grant / opportunity."),
    ("Organisation", "org", "Applicant or partner organisation."),
    ("Summary", "summary", "One or two sentences on what the grant would fund."),
    ("Stage", "stage", "One of: " + ", ".join(STAGES) + ". Defaults to Inbound."),
    ("Reach (1-5)", "reach", "How many people would this affect? 1 = handful, 5 = thousands+."),
    ("Impact (1-5)", "impact", "For those people, how big is the effect? 1 = minor, 5 = transformational."),
    ("Confidence (%)", "confidence", "How sure are we it will deliver? 0-100. 50 = a bet, 90 = near-certain."),
    ("Effort (1-5)", "effort", "How far are we from realising it? 1 = trivial, 5 = very heavy lift."),
    ("Amount", "amount", "Requested amount (number, no currency symbol)."),
    ("Deadline (YYYY-MM-DD)", "deadline", "Application or decision deadline."),
    ("Primary Contact Name", "contact_name", "Main person we deal with."),
    ("Primary Contact Email", "contact_email", "Their email address."),
    ("Other Contacts", "other_contacts", "Semicolon-separated: Name <email>; Name <email>"),
    ("Last Interaction (YYYY-MM-DD)", "last_interaction", "Date of the most recent email/meeting. Defaults to today."),
    ("Ball In Court", "court", "Ours or Theirs. Ours = waiting on us to act."),
    ("Starred (Y/N)", "starred", "Y to favourite this grant."),
    ("Notes", "notes", "Anything else."),
]


# --------------------------------------------------------------------------- DB
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS grants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            org TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            stage TEXT DEFAULT 'Inbound',
            reach INTEGER DEFAULT 3,
            impact INTEGER DEFAULT 3,
            confidence INTEGER DEFAULT 50,
            effort INTEGER DEFAULT 3,
            amount REAL,
            deadline TEXT,
            court TEXT DEFAULT 'Ours',
            court_override INTEGER DEFAULT 0,
            starred INTEGER DEFAULT 0,
            notes TEXT DEFAULT '',
            source_text TEXT DEFAULT '',
            last_interaction TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            grant_id INTEGER NOT NULL REFERENCES grants(id) ON DELETE CASCADE,
            name TEXT DEFAULT '',
            email TEXT DEFAULT '',
            role TEXT DEFAULT '',
            is_primary INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            grant_id INTEGER NOT NULL REFERENCES grants(id) ON DELETE CASCADE,
            direction TEXT NOT NULL,          -- 'in' (they acted / ball to us) or 'out' (we acted / ball to them)
            kind TEXT DEFAULT 'email',        -- email | meeting | call | note
            status TEXT DEFAULT 'logged',     -- logged | queued (outbound not actually sent)
            sender TEXT DEFAULT '',
            recipients TEXT DEFAULT '',
            subject TEXT DEFAULT '',
            body TEXT DEFAULT '',
            sent_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS thread_summaries (
            grant_id INTEGER NOT NULL,
            thread_key TEXT NOT NULL,
            msg_count INTEGER,
            summary TEXT,
            PRIMARY KEY (grant_id, thread_key)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)
        cols = [r["name"] for r in c.execute("PRAGMA table_info(messages)")]
        if "kind" not in cols:
            c.execute("ALTER TABLE messages ADD COLUMN kind TEXT DEFAULT 'email'")


def get_setting(key, default=""):
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_setting(key, value):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ---------------------------------------------------------------------- helpers
def today():
    return date.today().isoformat()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def days_since(iso):
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso[:19]).date()
    except ValueError:
        return None
    return (date.today() - d).days


def thread_key(subject):
    s = (subject or "").strip()
    s = re.sub(r"^(?:(re|fw|fwd|aw|wg)\s*:\s*)+", "", s, flags=re.I)
    return s.lower() or "(no subject)"


def score(g):
    conf = (g["confidence"] or 0) / 100.0
    eff = max(1, g["effort"] or 1)
    return round((g["reach"] or 0) * (g["impact"] or 0) * conf / eff, 2)


def grant_row(c, g, with_children=False):
    d = dict(g)
    d["score"] = score(g)
    d["days_idle"] = days_since(g["last_interaction"])
    d["awaiting_action"] = (g["court"] == "Ours") and g["stage"] not in CLOSED
    d["stage_index"] = STAGES.index(g["stage"]) if g["stage"] in STAGES else 0
    d["starred"] = bool(g["starred"])
    cnt = c.execute("SELECT COUNT(*) n FROM contacts WHERE grant_id=?", (g["id"],)).fetchone()["n"]
    d["contact_count"] = cnt
    if with_children:
        d["contacts"] = [dict(r) for r in c.execute(
            "SELECT * FROM contacts WHERE grant_id=? ORDER BY is_primary DESC, name", (g["id"],))]
        msgs = [dict(r) for r in c.execute(
            "SELECT * FROM messages WHERE grant_id=? ORDER BY sent_at, id", (g["id"],))]
        d["messages"] = msgs
        d["threads"] = build_threads(c, g["id"], msgs)
    return d


def build_threads(c, grant_id, msgs):
    threads = {}
    for m in msgs:
        kind = m.get("kind") or "email"
        if kind == "email":
            k = thread_key(m["subject"])
            label = m["subject"] or "(no subject)"
        else:
            k = f"{kind}:{m['id']}"
            label = m["subject"] or f"{kind.title()} on {m['sent_at'][:10]}"
        t = threads.setdefault(k, {"key": k, "kind": kind, "subject": label,
                                   "messages": [], "last_at": m["sent_at"], "summary": ""})
        t["messages"].append(m)
        t["last_at"] = m["sent_at"]
    out = sorted(threads.values(), key=lambda t: t["last_at"], reverse=True)
    for t in out:
        r = c.execute("SELECT msg_count, summary FROM thread_summaries WHERE grant_id=? AND thread_key=?",
                      (grant_id, t["key"])).fetchone()
        if r and r["msg_count"] == len(t["messages"]) and r["summary"]:
            t["summary"] = r["summary"]
        else:
            t["summary"] = heuristic_thread_summary(t)
            t["summary_source"] = "heuristic"
    return out


def heuristic_thread_summary(t):
    msgs = t["messages"]
    last = msgs[-1]
    if t.get("kind") != "email":
        body = re.sub(r"\s+", " ", (last["body"] or "")).strip()
        who = {"in": "ball came to us", "out": "ball went to them", "note": "no action change"}.get(last["direction"], "")
        return f"{t['kind'].title()} — {who}. {body[:160]}"
    n_in = sum(1 for m in msgs if m["direction"] == "in")
    n_out = len(msgs) - n_in
    who = "They" if last["direction"] == "in" else "We"
    first_line = re.sub(r"\s+", " ", (last["body"] or "")).strip()
    first_line = (first_line[:140] + "…") if len(first_line) > 140 else first_line
    ago = days_since(last["sent_at"])
    return (f"{len(msgs)} message{'s' if len(msgs) != 1 else ''} ({n_in} in, {n_out} out). "
            f"{who} wrote last{f', {ago}d ago' if ago is not None else ''}: “{first_line}”")


def recompute_court(c, grant_id):
    """Ball is in our court if the last message was inbound (or nothing sent yet)."""
    g = c.execute("SELECT court_override FROM grants WHERE id=?", (grant_id,)).fetchone()
    if not g or g["court_override"]:
        return
    last_any = c.execute("SELECT sent_at FROM messages WHERE grant_id=? ORDER BY sent_at DESC, id DESC LIMIT 1",
                         (grant_id,)).fetchone()
    last = c.execute("SELECT direction, sent_at FROM messages WHERE grant_id=? AND direction IN ('in','out') "
                     "ORDER BY sent_at DESC, id DESC LIMIT 1", (grant_id,)).fetchone()
    if last_any:
        c.execute("UPDATE grants SET last_interaction=?, updated_at=datetime('now') WHERE id=?",
                  (last_any["sent_at"][:10], grant_id))
    if last:
        court = "Ours" if last["direction"] == "in" else "Theirs"
        c.execute("UPDATE grants SET court=? WHERE id=?", (court, grant_id))


INT_FIELDS = {"reach": (1, 5), "impact": (1, 5), "effort": (1, 5), "confidence": (0, 100)}
TEXT_FIELDS = {"name", "org", "summary", "stage", "deadline", "notes", "source_text",
               "last_interaction", "court"}


def clean_grant_payload(p):
    out = {}
    for k, (lo, hi) in INT_FIELDS.items():
        if k in p and p[k] not in (None, ""):
            try:
                out[k] = max(lo, min(hi, int(round(float(p[k])))))
            except (TypeError, ValueError):
                pass
    for k in TEXT_FIELDS:
        if k in p and p[k] is not None:
            out[k] = str(p[k]).strip()
    if "stage" in out and out["stage"] not in STAGES:
        # tolerate case/space differences
        m = [s for s in STAGES if s.lower().replace(" ", "") == out["stage"].lower().replace(" ", "")]
        out["stage"] = m[0] if m else "Inbound"
    if "court" in out:
        out["court"] = "Theirs" if out["court"].lower().startswith("t") else "Ours"
    if "amount" in p:
        try:
            out["amount"] = float(str(p["amount"]).replace(",", "").replace("$", "").replace("£", "").replace("€", "")) if p["amount"] not in (None, "") else None
        except ValueError:
            pass
    if "starred" in p:
        v = p["starred"]
        out["starred"] = 1 if (v is True or str(v).strip().lower() in ("1", "y", "yes", "true", "★")) else 0
    if "court_override" in p:
        out["court_override"] = 1 if p["court_override"] else 0
    return out


# ------------------------------------------------------------- xlsx (no deps)
def col_letter(n):  # 0 -> A
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def xlsx_cell(ref, value, style=0):
    if value is None or value == "":
        return f'<c r="{ref}" s="{style}"/>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
    return f'<c r="{ref}" t="inlineStr" s="{style}"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'


def sheet_xml(rows, widths=None, validations="", freeze_header=True):
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
    if freeze_header:
        out.append('<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
                   'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>')
    if widths:
        out.append("<cols>" + "".join(
            f'<col min="{i+1}" max="{i+1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths)) + "</cols>")
    out.append("<sheetData>")
    for ri, row in enumerate(rows):
        cells = []
        for ci, v in enumerate(row):
            cells.append(xlsx_cell(f"{col_letter(ci)}{ri+1}", v, 1 if ri == 0 else 0))
        out.append(f'<row r="{ri+1}">' + "".join(cells) + "</row>")
    out.append("</sheetData>")
    if validations:
        out.append(validations)
    out.append("</worksheet>")
    return "".join(out)


def build_xlsx(sheets):
    """sheets: list of (name, xml_string). Returns bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i+1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(len(sheets)))
                   + "</Types>")
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   '</Relationships>')
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{escape(n)}" sheetId="{i+1}" r:id="rId{i+1}"/>' for i, (n, _) in enumerate(sheets))
                   + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i+1}.xml"/>'
                             for i in range(len(sheets)))
                   + f'<Relationship Id="rId{len(sheets)+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   '</Relationships>')
        z.writestr("xl/styles.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
                   '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts>'
                   '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
                   '<fill><patternFill patternType="solid"><fgColor rgb="FF1F4E5F"/></patternFill></fill></fills>'
                   '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                   '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                   '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
                   '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>'
                   '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
                   '</styleSheet>')
        for i, (_, xml) in enumerate(sheets):
            z.writestr(f"xl/worksheets/sheet{i+1}.xml", xml)
    return buf.getvalue()


def template_xlsx(example_rows=None):
    headers = [h for h, _, _ in TEMPLATE_COLUMNS]
    rows = [headers]
    if example_rows:
        rows += example_rows
    else:
        rows.append(["Community Garden Expansion", "Greenways Trust", "Extend the Riverside garden to 40 new plots.",
                     "Prospect", 3, 4, 60, 2, 25000, "2027-01-31", "Maya Patel", "maya@greenways.example",
                     "Tom Reilly <tom@greenways.example>", today(), "Ours", "Y", "Came in via website form."])
    widths = [30, 22, 40, 16, 12, 12, 14, 12, 12, 20, 22, 28, 36, 26, 14, 12, 40]
    stage_col = col_letter(3)
    court_col = col_letter(14)
    star_col = col_letter(15)
    validations = (
        '<dataValidations count="3">'
        f'<dataValidation type="list" allowBlank="1" showDropDown="0" sqref="{stage_col}2:{stage_col}1000">'
        f'<formula1>"{",".join(STAGES)}"</formula1></dataValidation>'
        f'<dataValidation type="list" allowBlank="1" sqref="{court_col}2:{court_col}1000"><formula1>"Ours,Theirs"</formula1></dataValidation>'
        f'<dataValidation type="list" allowBlank="1" sqref="{star_col}2:{star_col}1000"><formula1>"Y,N"</formula1></dataValidation>'
        '</dataValidations>')
    guide = [["Column", "What to put in it"]] + [[h, help_] for h, _, help_ in TEMPLATE_COLUMNS]
    guide += [[], ["Tips", ""],
              ["", "Only 'Grant Name' is required. Everything else can be filled in later inside GrantDesk."],
              ["", "Keep the header row exactly as-is so the import can match columns automatically."],
              ["", "Working in Google Sheets? File → Download → Microsoft Excel (.xlsx), then upload that file."],
              ["", "Scoring: Score = Reach × Impact × Confidence ÷ Effort (a RICE-style formula)."]]
    return build_xlsx([("Grants", sheet_xml(rows, widths, validations)),
                       ("How to fill this in", sheet_xml(guide, [30, 100], freeze_header=False))])


def _xml_strip_ns(tag):
    return tag.split("}", 1)[-1]


def read_xlsx(data):
    """Return list of rows (list of str/float) from the first worksheet."""
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    # shared strings
    shared = []
    if "xl/sharedStrings.xml" in names:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root:
            if _xml_strip_ns(si.tag) != "si":
                continue
            shared.append("".join(t.text or "" for t in si.iter() if _xml_strip_ns(t.tag) == "t"))
    # first sheet via workbook rels
    sheet_path = None
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rid_to_target = {}
        for r in rels:
            rid_to_target[r.attrib.get("Id")] = r.attrib.get("Target")
        for el in wb.iter():
            if _xml_strip_ns(el.tag) == "sheet":
                rid = [v for k, v in el.attrib.items() if k.endswith("}id") or k == "r:id"][0]
                target = rid_to_target[rid]
                sheet_path = target if target.startswith("xl/") else "xl/" + target.lstrip("/")
                break
    except Exception:
        pass
    if not sheet_path or sheet_path not in names:
        cands = sorted(n for n in names if n.startswith("xl/worksheets/sheet"))
        sheet_path = cands[0]
    # date styles
    date_styles = set()
    if "xl/styles.xml" in names:
        try:
            st = ET.fromstring(z.read("xl/styles.xml"))
            numfmts = {}
            for el in st.iter():
                if _xml_strip_ns(el.tag) == "numFmt":
                    numfmts[el.attrib["numFmtId"]] = el.attrib.get("formatCode", "")
            xfs = [el for el in st.iter() if _xml_strip_ns(el.tag) == "cellXfs"]
            if xfs:
                for i, xf in enumerate(list(xfs[0])):
                    fid = xf.attrib.get("numFmtId", "0")
                    code = numfmts.get(fid, "")
                    if fid in {"14", "15", "16", "17", "22"} or re.search(r"[dmy]{2,}", code.lower()):
                        date_styles.add(i)
        except Exception:
            pass
    root = ET.fromstring(z.read(sheet_path))
    rows = []
    for row in root.iter():
        if _xml_strip_ns(row.tag) != "row":
            continue
        cells = {}
        for c in row:
            if _xml_strip_ns(c.tag) != "c":
                continue
            ref = c.attrib.get("r", "")
            col = re.match(r"[A-Z]+", ref)
            idx = 0
            if col:
                for ch in col.group(0):
                    idx = idx * 26 + (ord(ch) - 64)
                idx -= 1
            else:
                idx = len(cells)
            t = c.attrib.get("t")
            v = None
            if t == "inlineStr":
                v = "".join(x.text or "" for x in c.iter() if _xml_strip_ns(x.tag) == "t")
            else:
                vel = [x for x in c if _xml_strip_ns(x.tag) == "v"]
                if vel:
                    raw = vel[0].text or ""
                    if t == "s":
                        v = shared[int(raw)] if raw.isdigit() and int(raw) < len(shared) else ""
                    elif t == "b":
                        v = "Y" if raw == "1" else "N"
                    elif t == "str" or t == "e":
                        v = raw
                    else:
                        try:
                            num = float(raw)
                            s = int(c.attrib.get("s", "0") or 0)
                            if s in date_styles and 1 < num < 100000:
                                v = (date(1899, 12, 30) + timedelta(days=int(num))).isoformat()
                            else:
                                v = int(num) if num.is_integer() else num
                        except ValueError:
                            v = raw
            cells[idx] = v
        if cells:
            width = max(cells) + 1
            rows.append([cells.get(i) for i in range(width)])
    return rows


def read_csv_bytes(data):
    text = data.decode("utf-8-sig", errors="replace")
    return [row for row in csv.reader(io.StringIO(text))]


def norm_header(h):
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower().split("(")[0])


HEADER_ALIASES = {
    "name": ["grantname", "grant", "name", "title", "opportunity", "project"],
    "org": ["organisation", "organization", "org", "applicant", "partner", "funder"],
    "summary": ["summary", "description", "about"],
    "stage": ["stage", "status", "phase", "step"],
    "reach": ["reach", "beneficiaries", "people"],
    "impact": ["impact", "effect"],
    "confidence": ["confidence", "likelihood", "probability"],
    "effort": ["effort", "difficulty", "work"],
    "amount": ["amount", "value", "budget", "ask", "requested", "size"],
    "deadline": ["deadline", "due", "duedate", "closes", "closingdate"],
    "contact_name": ["primarycontactname", "contact", "contactname", "leadcontact", "owner"],
    "contact_email": ["primarycontactemail", "email", "contactemail"],
    "other_contacts": ["othercontacts", "contacts", "cc", "team"],
    "last_interaction": ["lastinteraction", "lastcontact", "lasttouch", "lastemail", "lastupdated"],
    "court": ["ballincourt", "court", "waitingon", "whoseturn", "owner"],
    "starred": ["starred", "star", "favourite", "favorite", "flag"],
    "notes": ["notes", "comments", "remarks"],
}


def guess_mapping(headers):
    mapping = {}
    used = set()
    normed = [norm_header(h) for h in headers]
    for field, aliases in HEADER_ALIASES.items():
        for a in aliases:
            for i, n in enumerate(normed):
                if i in used:
                    continue
                if n == a:
                    mapping[field] = i
                    used.add(i)
                    break
            if field in mapping:
                break
    return mapping


def parse_contacts_str(s):
    out = []
    for part in re.split(r"[;\n]+", s or ""):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(.*?)\s*<([^>]+)>\s*$", part)
        if m:
            out.append({"name": m.group(1).strip(" \"'"), "email": m.group(2).strip()})
        elif "@" in part:
            out.append({"name": part.split("@")[0].replace(".", " ").title(), "email": part})
        else:
            out.append({"name": part, "email": ""})
    return out


def import_rows(rows, mapping, mode="append"):
    """rows: list of lists incl. header row; mapping: field -> column index."""
    created = 0
    skipped = 0
    with db() as c:
        if mode == "replace":
            c.execute("DELETE FROM grants")
            c.execute("DELETE FROM thread_summaries")
        for row in rows[1:]:
            def get(field):
                i = mapping.get(field)
                if i is None or i >= len(row):
                    return None
                v = row[i]
                return v if v is not None else None
            name = (str(get("name") or "")).strip()
            if not name:
                skipped += 1
                continue
            payload = {f: get(f) for f in ("org", "summary", "stage", "reach", "impact", "confidence",
                                             "effort", "amount", "deadline", "notes", "last_interaction",
                                             "court", "starred")}
            payload["name"] = name
            conf = payload.get("confidence")
            if conf not in (None, "") and float(str(conf).rstrip("%")) <= 1.0 and "%" not in str(conf):
                payload["confidence"] = float(conf) * 100
            elif isinstance(conf, str):
                payload["confidence"] = conf.rstrip("%")
            for dk in ("deadline", "last_interaction"):
                v = payload.get(dk)
                if isinstance(v, (int, float)) and 1 < v < 100000:
                    payload[dk] = (date(1899, 12, 30) + timedelta(days=int(v))).isoformat()
            clean = clean_grant_payload(payload)
            clean.setdefault("stage", "Inbound")
            clean.setdefault("court", "Ours")
            clean["court_override"] = 1 if get("court") not in (None, "") else 0
            clean.setdefault("last_interaction", today())
            if not clean.get("last_interaction"):
                clean["last_interaction"] = today()
            cols = ", ".join(clean.keys())
            qs = ", ".join("?" for _ in clean)
            cur = c.execute(f"INSERT INTO grants ({cols}) VALUES ({qs})", list(clean.values()))
            gid = cur.lastrowid
            cn, ce = (str(get("contact_name") or "")).strip(), (str(get("contact_email") or "")).strip()
            if cn or ce:
                c.execute("INSERT INTO contacts(grant_id,name,email,is_primary) VALUES(?,?,?,1)",
                          (gid, cn or ce.split("@")[0], ce))
            for k in parse_contacts_str(str(get("other_contacts") or "")):
                c.execute("INSERT INTO contacts(grant_id,name,email,is_primary) VALUES(?,?,?,0)",
                          (gid, k["name"], k["email"]))
            created += 1
    return {"created": created, "skipped": skipped}


def export_xlsx():
    with db() as c:
        rows = []
        for g in c.execute("SELECT * FROM grants ORDER BY id"):
            prim = c.execute("SELECT name,email FROM contacts WHERE grant_id=? AND is_primary=1 LIMIT 1", (g["id"],)).fetchone()
            others = c.execute("SELECT name,email FROM contacts WHERE grant_id=? AND is_primary=0", (g["id"],)).fetchall()
            rows.append([g["name"], g["org"], g["summary"], g["stage"], g["reach"], g["impact"], g["confidence"],
                         g["effort"], g["amount"], g["deadline"], prim["name"] if prim else "",
                         prim["email"] if prim else "", "; ".join(f"{o['name']} <{o['email']}>" for o in others),
                         g["last_interaction"], g["court"], "Y" if g["starred"] else "N", g["notes"]])
    return template_xlsx(rows)


# ---------------------------------------------------------------- email parse
def parse_pasted_email(text):
    """Pull a prospect out of a pasted email with regexes. Used when no LLM key."""
    emails = re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    seen, contacts = set(), []
    for e in emails:
        el = e.lower().rstrip(".")
        if el in seen:
            continue
        seen.add(el)
        # try to find "Name <email>" pattern
        m = re.search(r"([A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+){0,3})\s*<" + re.escape(e) + ">", text)
        name = m.group(1) if m else el.split("@")[0].replace(".", " ").replace("_", " ").title()
        contacts.append({"name": name, "email": el})
    subj = re.search(r"^\s*subject\s*:\s*(.+)$", text, re.I | re.M)
    frm = re.search(r"^\s*from\s*:\s*(.+)$", text, re.I | re.M)
    org = ""
    if contacts:
        dom = contacts[0]["email"].split("@")[1].split(".")[0]
        if dom not in ("gmail", "yahoo", "hotmail", "outlook", "icloud", "example"):
            org = dom.replace("-", " ").title()
    om = re.search(r"\b(?:at|from|on behalf of|representing)\s+([A-Z][\w&'’-]+(?:\s+[A-Z][\w&'’-]+){0,4})", text)
    if om:
        org = om.group(1)
    name = subj.group(1).strip() if subj else ""
    name = re.sub(r"^(?:(re|fw|fwd)\s*:\s*)+", "", name, flags=re.I)
    if not name:
        first = next((ln.strip() for ln in text.splitlines() if ln.strip() and not re.match(r"^(from|to|cc|date|sent|subject)\s*:", ln, re.I)), "")
        name = first[:80]
    amt = re.search(r"[£$€]\s?([\d,]+(?:\.\d+)?)\s*(k|m)?", text, re.I)
    amount = None
    if amt:
        amount = float(amt.group(1).replace(",", ""))
        if amt.group(2):
            amount *= 1000 if amt.group(2).lower() == "k" else 1_000_000
    body = re.sub(r"^(from|to|cc|date|sent|subject)\s*:.*$", "", text, flags=re.I | re.M).strip()
    summary = re.sub(r"\s+", " ", body)[:240]
    return {"name": name or "New prospect", "org": org, "summary": summary, "amount": amount,
            "contacts": contacts, "from": frm.group(1).strip() if frm else "",
            "subject": subj.group(1).strip() if subj else name}


# ------------------------------------------------------------------- LLM hook
def llm_available():
    return bool(get_setting("api_key"))


def llm(prompt, system="You are a concise assistant helping a small grant-making team.", max_tokens=600):
    key = get_setting("api_key")
    if not key:
        raise RuntimeError("no api key")
    model = get_setting("model") or "claude-sonnet-4-5"
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps({"model": model, "max_tokens": max_tokens, "system": system,
                         "messages": [{"role": "user", "content": prompt}]}).encode(),
        headers={"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"},
        method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    return "".join(b.get("text", "") for b in data.get("content", []))


def grant_context(d):
    lines = [f"Grant: {d['name']} — {d['org']}", f"Summary: {d['summary']}", f"Stage: {d['stage']}",
             f"Score {d['score']} (Reach {d['reach']}, Impact {d['impact']}, Confidence {d['confidence']}%, Effort {d['effort']})",
             f"Ball in court: {d['court']}; days since last interaction: {d['days_idle']}",
             f"Deadline: {d['deadline'] or 'none'}; Amount: {d['amount'] or 'n/a'}", f"Notes: {d['notes']}",
             "Contacts: " + ", ".join(f"{k['name']} <{k['email']}> {k['role']}" for k in d["contacts"]), "Recent emails:"]
    for m in d["messages"][-8:]:
        lines.append(f"  [{m['sent_at'][:10]}] {(m.get('kind') or 'email').upper()} {'IN ' if m['direction']=='in' else 'OUT' if m['direction']=='out' else 'NOTE'} {m['sender']} → {m['recipients']} | {m['subject']}\n    {(m['body'] or '')[:400]}")
    return "\n".join(lines)


def heuristic_suggestion(d):
    idle = d["days_idle"] or 0
    st = d["stage"]
    tips = []
    if st in CLOSED:
        return {"headline": f"Closed as {st}.", "steps": ["Archive the thread and record outcomes for the annual report."], "source": "heuristic"}
    if d["court"] == "Ours":
        if idle >= 7:
            tips.append(f"They are waiting on us and it has been {idle} days — reply today, even if only to acknowledge.")
        else:
            tips.append("The ball is in our court. Reply so the clock starts on their side.")
    else:
        if idle >= 14:
            tips.append(f"No word from them in {idle} days — send a short, friendly nudge with a specific ask.")
        elif idle >= 7:
            tips.append("Give it a few more days, then nudge if nothing arrives.")
        else:
            tips.append("Waiting on them; nothing to do yet.")
    next_stage = {"Inbound": "Score it (RICE) and decide whether to move to Prospect or decline.",
                  "Prospect": "Book the first meeting — propose 2–3 time slots.",
                  "First Meeting": "Request the documents you need for due diligence (budget, governance, references).",
                  "Due Diligence": "Share findings internally and schedule the second meeting.",
                  "Second Meeting": "Agree the scope for the deep dive and who owns each workstream.",
                  "Deep Dive": "Draft the recommendation and move to paperwork if it holds up.",
                  "Paperwork": "Chase signatures; confirm payment schedule and reporting dates."}.get(st, "")
    if next_stage:
        tips.append(next_stage)
    if d["score"] < 2 and st not in ("Inbound",):
        tips.append(f"Score is low ({d['score']}). Consider whether this is worth the effort or should be declined politely.")
    if d["deadline"]:
        dd = days_since(d["deadline"])
        if dd is not None and dd > -14 and dd <= 0:
            tips.append(f"Deadline is in {-dd} days — make sure the application is on track.")
        elif dd is not None and dd > 0:
            tips.append("Deadline has passed — confirm whether it was submitted or should be declined.")
    if d["contact_count"] == 0:
        tips.append("No contacts recorded — add at least one so emails can be logged against the right person.")
    return {"headline": tips[0], "steps": tips[1:], "source": "heuristic"}


# ------------------------------------------------------------------ seed data
def seed():
    t = date.today()
    def d(n):
        return (t - timedelta(days=n)).isoformat()
    def ts(n, h=10):
        return (datetime.combine(t, datetime.min.time()) - timedelta(days=n) + timedelta(hours=h)).strftime("%Y-%m-%d %H:%M:%S")
    us = "grants@brightfutures.example"
    G = []
    def grant(name, org, summary, stage, r, i, cf, e, amount, deadline, notes, starred, contacts, msgs, court_override=None):
        G.append(dict(name=name, org=org, summary=summary, stage=stage, reach=r, impact=i, confidence=cf, effort=e,
                      amount=amount, deadline=deadline, notes=notes, starred=starred, contacts=contacts, msgs=msgs,
                      court_override=court_override))
    grant("Riverside Community Garden Expansion", "Greenways Trust",
          "Extend the Riverside allotment site by 40 plots and a shared tool library.", "Due Diligence",
          3, 4, 70, 2, 25000, (t + timedelta(days=40)).isoformat(), "Strong volunteer base. Need land lease confirmation.", 1,
          [("Maya Patel", "maya@greenways.example", "Programme lead", 1), ("Tom Reilly", "tom@greenways.example", "Treasurer", 0)],
          [("in", 21, "Maya Patel <maya@greenways.example>", us, "Riverside garden expansion — funding enquiry",
            "Hello, we run the Riverside allotments and have a waiting list of 60 families. We'd like to apply for £25k to add 40 plots. Could we talk?"),
           ("out", 19, us, "maya@greenways.example", "Re: Riverside garden expansion — funding enquiry",
            "Thanks Maya — this looks like a great fit. Could you send the latest accounts and the lease agreement for the land?"),
           ("in", 4, "Maya Patel <maya@greenways.example>", us, "Re: Riverside garden expansion — funding enquiry",
            "Accounts attached. The lease is being renewed by the council; I'll have it within two weeks. Happy to meet whenever suits.")])
    grant("Youth Coding Bootcamp", "CodeForward CIC",
          "12-week evening coding programme for 60 young people in Northfield.", "Second Meeting",
          4, 4, 60, 3, 48000, (t + timedelta(days=25)).isoformat(), "Second meeting held, waiting on their revised budget.", 1,
          [("Dev Okafor", "dev@codeforward.example", "Founder", 1), ("Lena Brooks", "lena@codeforward.example", "Ops", 0)],
          [("in", 40, "Dev Okafor <dev@codeforward.example>", us, "Bootcamp proposal", "Please find our proposal for a 12-week bootcamp attached."),
           ("out", 38, us, "dev@codeforward.example", "Re: Bootcamp proposal", "Thanks Dev, let's set up a call next week."),
           ("in", 30, "Dev Okafor <dev@codeforward.example>", us, "Re: Bootcamp proposal", "Great — Tuesday 2pm works."),
           ("meeting", 13, "Second meeting with CodeForward", "Met Dev and Lena at their Northfield space. Walked through the curriculum; agreed they will revise the budget. Positive — strong safeguarding culture.", "out"),
           ("out", 12, us, "dev@codeforward.example", "Follow-up from our second meeting",
            "Good to see you both. As discussed, could you revise the budget to show laptop costs separately and send the safeguarding policy?"),
           ("in", 9, "Dev Okafor <dev@codeforward.example>", us, "Re: Follow-up from our second meeting",
            "Will do — our treasurer is away until next week so expect it in ~10 days.")], court_override=None)
    grant("Mobile Dental Clinic for Rural Villages", "Smile Outreach",
          "Equip a van to deliver check-ups to 12 villages with no dentist within 30 miles.", "Deep Dive",
          5, 5, 55, 4, 120000, (t + timedelta(days=60)).isoformat(), "Big ticket. Board needs to see the medical governance plan.", 1,
          [("Dr Hannah Cole", "hannah@smileoutreach.example", "Clinical director", 1)],
          [("in", 60, "Dr Hannah Cole <hannah@smileoutreach.example>", us, "Mobile dental clinic", "We would like to discuss a partnership for a mobile clinic."),
           ("out", 58, us, "hannah@smileoutreach.example", "Re: Mobile dental clinic", "Very interested. Meeting invite to follow."),
           ("call", 16, "Call with Dr Cole", "20-minute call to scope the deep dive. She is open to a two-year phasing.", "out"),
           ("note", 14, "Board sounding", "Chair is supportive in principle but wants clinical governance evidence before it goes to the full board.", "note"),
           ("out", 15, us, "hannah@smileoutreach.example", "Deep dive — documents needed",
            "To take this to the board we need: CQC registration evidence, the clinical governance plan, and 3-year running costs."),
           ("in", 2, "Dr Hannah Cole <hannah@smileoutreach.example>", us, "Re: Deep dive — documents needed",
            "All three attached. One question: would you consider funding the van purchase in year one and running costs in year two?")])
    grant("Warm Homes Winter Fund", "Northfield Advice Centre",
          "Emergency heating grants for 300 households this winter.", "Paperwork",
          4, 3, 90, 1, 30000, (t + timedelta(days=10)).isoformat(), "Approved in principle; agreement out for signature.", 0,
          [("Priya Nair", "priya@nac.example", "Director", 1)],
          [("out", 8, us, "priya@nac.example", "Grant agreement for signature", "Please sign and return the attached agreement by the 20th."),
           ("in", 1, "Priya Nair <priya@nac.example>", us, "Re: Grant agreement for signature", "Signed copy attached. Could you confirm the payment date?")])
    grant("Refugee Language Café", "Welcome Collective",
          "Weekly conversation café with childcare for 80 newly arrived adults.", "First Meeting",
          3, 3, 65, 2, 15000, None, "", 0,
          [("Samir Haddad", "samir@welcomecollective.example", "Coordinator", 1)],
          [("in", 18, "Samir Haddad <samir@welcomecollective.example>", us, "Language café — small grant?", "We've been running on donations and would love to put the café on a stable footing."),
           ("out", 16, us, "samir@welcomecollective.example", "Re: Language café — small grant?", "Happy to meet. Does Thursday at 11 work?"),
           ("in", 15, "Samir Haddad <samir@welcomecollective.example>", us, "Re: Language café — small grant?", "Thursday is perfect, see you then.")])
    grant("Repair Café Network", "Fixers United",
          "Launch five monthly repair cafés to cut waste and build skills.", "Prospect",
          3, 2, 50, 2, 9000, None, "Nice idea, modest impact.", 0,
          [("Jo Lindqvist", "jo@fixersunited.example", "Volunteer lead", 1)],
          [("in", 33, "Jo Lindqvist <jo@fixersunited.example>", us, "Repair cafés", "Would you fund tools and venue hire for five repair cafés?")])
    grant("Peer Support for New Parents", "Little Steps",
          "Train 20 peer supporters to run groups for isolated new parents.", "Prospect",
          3, 4, 55, 2, 18000, (t + timedelta(days=50)).isoformat(), "", 0,
          [("Aisha Bello", "aisha@littlesteps.example", "Founder", 1)],
          [("in", 12, "Aisha Bello <aisha@littlesteps.example>", us, "Peer support programme", "Sharing our concept note for peer-led parent groups."),
           ("out", 10, us, "aisha@littlesteps.example", "Re: Peer support programme", "Thanks Aisha — we'll review and come back to you with meeting dates.")])
    grant("Community Sports Hall Roof Repair", "Eastside Leisure Trust",
          "Replace the leaking roof so the hall can reopen for 1,200 weekly users.", "Inbound",
          5, 3, 80, 3, 75000, (t + timedelta(days=5)).isoformat(), "Urgent — hall closed since storm.", 0,
          [("Greg Mason", "greg@eastsideleisure.example", "Facilities", 1)],
          [("in", 3, "Greg Mason <greg@eastsideleisure.example>", us, "URGENT: sports hall roof", "The roof failed in last week's storm and the hall is closed. We need £75k for repairs. Can you help quickly?")])
    grant("Digital Skills for Over-65s", "Silver Surfers Club",
          "One-to-one tablet lessons for 150 older residents.", "Inbound",
          3, 3, 60, 2, 12000, None, "", 0,
          [("Margaret Hughes", "margaret@silversurfers.example", "Chair", 1)],
          [("in", 1, "Margaret Hughes <margaret@silversurfers.example>", us, "Digital lessons funding", "Our members would benefit hugely from help with online banking and video calls to family.")])
    grant("School Breakfast Club Expansion", "Hillcrest Primary PTA",
          "Extend the breakfast club from 2 to 5 days for 90 pupils.", "Due Diligence",
          3, 4, 85, 1, 8000, None, "Low effort, high confidence — quick win.", 1,
          [("Rachel Kim", "rachel.kim@hillcrestpta.example", "PTA chair", 1)],
          [("out", 20, us, "rachel.kim@hillcrestpta.example", "Breakfast club — a few questions", "Could you send attendance figures and the school's letter of support?"),
           ("in", 17, "Rachel Kim <rachel.kim@hillcrestpta.example>", us, "Re: Breakfast club — a few questions", "Both attached. Let me know if you need anything else.")])
    grant("Theatre Access Programme", "Stagelight Arts",
          "Subsidised tickets and transport for 400 disabled audience members.", "Declined",
          3, 2, 40, 3, 22000, None, "Outside our priority areas this year.", 0,
          [("Oliver Grant", "oliver@stagelight.example", "Development", 1)],
          [("in", 70, "Oliver Grant <oliver@stagelight.example>", us, "Access programme", "Proposal attached."),
           ("out", 55, us, "oliver@stagelight.example", "Re: Access programme", "Thank you for the proposal. Unfortunately it falls outside our priorities this year.")])
    grant("Food Bank Cold Storage", "Northfield Food Bank",
          "Walk-in fridge so the food bank can accept fresh produce donations.", "Awarded",
          4, 4, 95, 1, 14000, None, "Paid in full.", 0,
          [("Ben Carter", "ben@nfb.example", "Manager", 1)],
          [("out", 45, us, "ben@nfb.example", "Grant awarded — cold storage", "Delighted to confirm the award of £14,000."),
           ("in", 44, "Ben Carter <ben@nfb.example>", us, "Re: Grant awarded — cold storage", "Thank you so much! This will change what we can offer.")])
    grant("Men's Shed Woodworking Workshop", "Northfield Men's Shed",
          "Fit out a workshop for 40 members tackling loneliness through making.", "First Meeting",
          2, 3, 60, 2, 11000, None, "", 0,
          [("Frank Doyle", "frank@menshed.example", "Secretary", 1)],
          [("in", 28, "Frank Doyle <frank@menshed.example>", us, "Men's shed workshop", "We've secured a unit and need help with machinery and safety training."),
           ("out", 26, us, "frank@menshed.example", "Re: Men's shed workshop", "Thanks Frank, let's meet — how about next Wednesday?")])
    grant("River Clean-up Volunteer Kit", "Clearwater Volunteers",
          "Waders, grabbers and a trailer for monthly river clean-ups.", "Inbound",
          2, 2, 70, 1, 4000, None, "", 0,
          [("Sophie Turner", "sophie@clearwater.example", "Organiser", 1)],
          [("in", 6, "Sophie Turner <sophie@clearwater.example>", us, "Small kit grant", "A small grant would let us run clean-ups year-round.")])

    with db() as c:
        c.execute("DELETE FROM grants")
        c.execute("DELETE FROM thread_summaries")
        for g in G:
            cur = c.execute(
                "INSERT INTO grants(name,org,summary,stage,reach,impact,confidence,effort,amount,deadline,notes,starred,last_interaction,court) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (g["name"], g["org"], g["summary"], g["stage"], g["reach"], g["impact"], g["confidence"], g["effort"],
                 g["amount"], g["deadline"], g["notes"], g["starred"], today(), "Ours"))
            gid = cur.lastrowid
            for name, email, role, prim in g["contacts"]:
                c.execute("INSERT INTO contacts(grant_id,name,email,role,is_primary) VALUES(?,?,?,?,?)", (gid, name, email, role, prim))
            for m in g["msgs"]:
                if m[0] in ("meeting", "call", "note"):
                    kind, days_ago, subj, body, direction = m
                    c.execute("INSERT INTO messages(grant_id,direction,kind,status,sender,recipients,subject,body,sent_at) VALUES(?,?,?,?,?,?,?,?,?)",
                              (gid, direction, kind, "logged", "", "", subj, body, ts(days_ago)))
                else:
                    direction, days_ago, sender, rcpt, subj, body = m
                    c.execute("INSERT INTO messages(grant_id,direction,kind,status,sender,recipients,subject,body,sent_at) VALUES(?,?,?,?,?,?,?,?,?)",
                              (gid, direction, "email", "logged", sender, rcpt, subj, body, ts(days_ago)))
            recompute_court(c, gid)
    return len(G)


# ----------------------------------------------------------------------- HTTP
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            return
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- plumbing
    def send_json(self, obj, code=200):
        data = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_bytes(self, data, ctype, filename=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def read_json(self):
        raw = self.read_body()
        return json.loads(raw) if raw else {}

    def read_multipart(self):
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r"boundary=\"?([^\";]+)\"?", ctype)
        body = self.read_body()
        if not m:
            return {}, {}
        boundary = ("--" + m.group(1)).encode()
        fields, files = {}, {}
        for part in body.split(boundary):
            part = part.strip(b"\r\n")
            if not part or part == b"--":
                continue
            head, _, content = part.partition(b"\r\n\r\n")
            head = head.decode(errors="replace")
            nm = re.search(r'name="([^"]*)"', head)
            fn = re.search(r'filename="([^"]*)"', head)
            if not nm:
                continue
            if fn:
                files[nm.group(1)] = (fn.group(1), content)
            else:
                fields[nm.group(1)] = content.decode(errors="replace")
        return fields, files

    # -- routing
    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)
        try:
            if p == "/api/grants":
                with db() as c:
                    rows = [grant_row(c, g) for g in c.execute("SELECT * FROM grants ORDER BY id")]
                return self.send_json(rows)
            m = re.match(r"^/api/grants/(\d+)$", p)
            if m:
                with db() as c:
                    g = c.execute("SELECT * FROM grants WHERE id=?", (m.group(1),)).fetchone()
                    if not g:
                        return self.send_json({"error": "not found"}, 404)
                    return self.send_json(grant_row(c, g, with_children=True))
            m = re.match(r"^/api/grants/(\d+)/suggest$", p)
            if m:
                return self.send_json(self.suggest(int(m.group(1))))
            if p == "/api/template.xlsx":
                return self.send_bytes(template_xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                       "grantdesk_template.xlsx")
            if p == "/api/export.xlsx":
                return self.send_bytes(export_xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                       f"grantdesk_export_{today()}.xlsx")
            if p == "/api/export.json":
                with db() as c:
                    out = [grant_row(c, g, with_children=True) for g in c.execute("SELECT * FROM grants ORDER BY id")]
                return self.send_bytes(json.dumps(out, indent=2, default=str).encode(), "application/json",
                                       f"grantdesk_backup_{today()}.json")
            if p == "/api/settings":
                return self.send_json({"has_api_key": llm_available(), "model": get_setting("model") or "claude-sonnet-4-5",
                                       "our_email": get_setting("our_email") or "grants@yourorg.example",
                                       "org_name": get_setting("org_name") or "Our Foundation", "stages": STAGES})
            if p == "/api/stats":
                with db() as c:
                    rows = [grant_row(c, g) for g in c.execute("SELECT * FROM grants")]
                active = [r for r in rows if r["stage"] not in CLOSED]
                return self.send_json({"total": len(rows), "active": len(active),
                                       "awaiting": sum(1 for r in active if r["awaiting_action"]),
                                       "stale": sum(1 for r in active if (r["days_idle"] or 0) >= 14),
                                       "starred": sum(1 for r in rows if r["starred"])})
            if p.startswith("/api/"):
                return self.send_json({"error": "unknown endpoint"}, 404)
            if p == "/" or not os.path.exists(os.path.join(STATIC, p.lstrip("/"))):
                self.path = "/index.html"
            return super().do_GET()
        except Exception as e:  # noqa
            import traceback
            traceback.print_exc()
            return self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            if p == "/api/grants":
                body = self.read_json()
                clean = clean_grant_payload(body)
                if not clean.get("name"):
                    return self.send_json({"error": "name is required"}, 400)
                clean.setdefault("stage", "Inbound")
                clean.setdefault("court", "Ours")
                clean.setdefault("last_interaction", today())
                with db() as c:
                    cols = ", ".join(clean.keys())
                    cur = c.execute(f"INSERT INTO grants ({cols}) VALUES ({', '.join('?' for _ in clean)})", list(clean.values()))
                    gid = cur.lastrowid
                    for i, k in enumerate(body.get("contacts") or []):
                        if k.get("name") or k.get("email"):
                            c.execute("INSERT INTO contacts(grant_id,name,email,role,is_primary) VALUES(?,?,?,?,?)",
                                      (gid, k.get("name", ""), k.get("email", ""), k.get("role", ""), 1 if i == 0 else 0))
                    if body.get("source_text") and body.get("log_as_email", True):
                        parsed = parse_pasted_email(body["source_text"])
                        c.execute("INSERT INTO messages(grant_id,direction,status,sender,recipients,subject,body,sent_at) VALUES(?,?,?,?,?,?,?,?)",
                                  (gid, "in", "logged", parsed["from"] or (parsed["contacts"][0]["email"] if parsed["contacts"] else ""),
                                   get_setting("our_email") or "us", parsed["subject"] or clean["name"], body["source_text"], now()))
                        recompute_court(c, gid)
                    g = c.execute("SELECT * FROM grants WHERE id=?", (gid,)).fetchone()
                    return self.send_json(grant_row(c, g, True), 201)
            if p == "/api/parse-email":
                text = self.read_json().get("text", "")
                result = parse_pasted_email(text)
                result["source"] = "heuristic"
                if llm_available():
                    try:
                        out = llm("Extract a grant prospect from this pasted email. Return ONLY JSON with keys: "
                                  "name (short title), org, summary (<=2 sentences), amount (number or null), "
                                  "contacts (list of {name,email,role}), subject, reach(1-5), impact(1-5), confidence(0-100), effort(1-5).\n\n" + text,
                                  max_tokens=700)
                        j = json.loads(re.search(r"\{.*\}", out, re.S).group(0))
                        result.update({k: v for k, v in j.items() if v not in (None, "")})
                        result["source"] = "llm"
                    except Exception as e:  # fall back silently
                        result["llm_error"] = str(e)
                return self.send_json(result)
            m = re.match(r"^/api/grants/(\d+)/contacts$", p)
            if m:
                k = self.read_json()
                with db() as c:
                    c.execute("INSERT INTO contacts(grant_id,name,email,role,is_primary) VALUES(?,?,?,?,?)",
                              (m.group(1), k.get("name", ""), k.get("email", ""), k.get("role", ""), 1 if k.get("is_primary") else 0))
                return self.send_json({"ok": True}, 201)
            m = re.match(r"^/api/grants/(\d+)/messages$", p)
            if m:
                k = self.read_json()
                gid = int(m.group(1))
                kind = k.get("kind") if k.get("kind") in ("email", "meeting", "call", "note") else "email"
                direction = k.get("direction") if k.get("direction") in ("in", "out", "note") else "in"
                if kind == "note":
                    direction = "note"
                status = (k.get("status") if k.get("status") in ("queued", "sent") else "queued") if (direction == "out" and kind == "email") else "logged"
                with db() as c:
                    c.execute("INSERT INTO messages(grant_id,direction,kind,status,sender,recipients,subject,body,sent_at) VALUES(?,?,?,?,?,?,?,?,?)",
                              (gid, direction, kind, status, k.get("sender", ""), k.get("recipients", ""), k.get("subject", ""),
                               k.get("body", ""), k.get("sent_at") or now()))
                    recompute_court(c, gid)
                    g = c.execute("SELECT * FROM grants WHERE id=?", (gid,)).fetchone()
                    return self.send_json(grant_row(c, g, True), 201)
            m = re.match(r"^/api/grants/(\d+)/summarise$", p)
            if m:
                return self.send_json(self.summarise(int(m.group(1))))
            if p == "/api/import/preview":
                fields, files = self.read_multipart()
                if "file" not in files:
                    return self.send_json({"error": "no file"}, 400)
                fn, content = files["file"]
                rows = read_xlsx(content) if fn.lower().endswith((".xlsx", ".xlsm")) else read_csv_bytes(content)
                rows = [r for r in rows if any(v not in (None, "") for v in r)]
                if not rows:
                    return self.send_json({"error": "file is empty"}, 400)
                headers = [str(h or "") for h in rows[0]]
                return self.send_json({"filename": fn, "headers": headers, "rows": rows[:2000],
                                       "mapping": guess_mapping(headers), "fields": [(f, h) for h, f, _ in TEMPLATE_COLUMNS],
                                       "total": len(rows) - 1})
            if p == "/api/import/commit":
                body = self.read_json()
                res = import_rows(body["rows"], {k: int(v) for k, v in body["mapping"].items() if v not in (None, "")},
                                  body.get("mode", "append"))
                return self.send_json(res)
            if p == "/api/seed":
                return self.send_json({"ok": True, "created": seed()})
            if p == "/api/settings":
                body = self.read_json()
                for k in ("model", "our_email", "org_name"):
                    if k in body:
                        set_setting(k, body[k])
                if "api_key" in body:  # stored locally in grantdesk.db only; never logged
                    set_setting("api_key", body["api_key"].strip())
                return self.send_json({"ok": True, "has_api_key": llm_available()})
            return self.send_json({"error": "unknown endpoint"}, 404)
        except Exception as e:  # noqa
            import traceback
            traceback.print_exc()
            return self.send_json({"error": str(e)}, 500)

    def do_PATCH(self):
        p = urlparse(self.path).path
        try:
            m = re.match(r"^/api/grants/(\d+)$", p)
            if m:
                gid = int(m.group(1))
                body = self.read_json()
                clean = clean_grant_payload(body)
                if "court" in clean and "court_override" not in clean:
                    clean["court_override"] = 1
                with db() as c:
                    if clean:
                        sets = ", ".join(f"{k}=?" for k in clean) + ", updated_at=datetime('now')"
                        c.execute(f"UPDATE grants SET {sets} WHERE id=?", list(clean.values()) + [gid])
                    if body.get("court_override") is False:
                        c.execute("UPDATE grants SET court_override=0 WHERE id=?", (gid,))
                        recompute_court(c, gid)
                    g = c.execute("SELECT * FROM grants WHERE id=?", (gid,)).fetchone()
                    if not g:
                        return self.send_json({"error": "not found"}, 404)
                    return self.send_json(grant_row(c, g, with_children=bool(body.get("_full"))))
            m = re.match(r"^/api/contacts/(\d+)$", p)
            if m:
                k = self.read_json()
                with db() as c:
                    c.execute("UPDATE contacts SET name=?, email=?, role=? WHERE id=?",
                              (k.get("name", ""), k.get("email", ""), k.get("role", ""), m.group(1)))
                return self.send_json({"ok": True})
            return self.send_json({"error": "unknown endpoint"}, 404)
        except Exception as e:  # noqa
            return self.send_json({"error": str(e)}, 500)

    def do_DELETE(self):
        p = urlparse(self.path).path
        with db() as c:
            m = re.match(r"^/api/grants/(\d+)$", p)
            if m:
                c.execute("DELETE FROM grants WHERE id=?", (m.group(1),))
                return self.send_json({"ok": True})
            m = re.match(r"^/api/contacts/(\d+)$", p)
            if m:
                c.execute("DELETE FROM contacts WHERE id=?", (m.group(1),))
                return self.send_json({"ok": True})
            m = re.match(r"^/api/messages/(\d+)$", p)
            if m:
                r = c.execute("SELECT grant_id FROM messages WHERE id=?", (m.group(1),)).fetchone()
                c.execute("DELETE FROM messages WHERE id=?", (m.group(1),))
                if r:
                    recompute_court(c, r["grant_id"])
                return self.send_json({"ok": True})
        return self.send_json({"error": "unknown endpoint"}, 404)

    # -- AI-ish
    def suggest(self, gid):
        with db() as c:
            g = c.execute("SELECT * FROM grants WHERE id=?", (gid,)).fetchone()
            if not g:
                return {"error": "not found"}
            d = grant_row(c, g, True)
        base = heuristic_suggestion(d)
        if llm_available():
            try:
                out = llm("Here is a grant in our pipeline:\n\n" + grant_context(d) +
                          "\n\nPropose the single best next action (one sentence, 'headline') and up to 3 concrete follow-up "
                          "steps ('steps'). Also propose a one-line draft subject + 2-sentence email body if an email is the "
                          "next step ('draft_subject', 'draft_body'); otherwise null. Return ONLY JSON.", max_tokens=600)
                j = json.loads(re.search(r"\{.*\}", out, re.S).group(0))
                j["source"] = "llm"
                return j
            except Exception as e:
                base["llm_error"] = str(e)
        return base

    def summarise(self, gid):
        """Summarise every thread with the LLM and cache. Falls back to heuristics."""
        with db() as c:
            g = c.execute("SELECT * FROM grants WHERE id=?", (gid,)).fetchone()
            if not g:
                return {"error": "not found"}
            d = grant_row(c, g, True)
        if not llm_available():
            return {"ok": False, "reason": "no api key", "threads": d["threads"]}
        with db() as c:
            for t in d["threads"]:
                convo = "\n\n".join(f"[{m['sent_at'][:10]}] {'THEM' if m['direction']=='in' else 'US'}: {m['body']}" for m in t["messages"])
                try:
                    s = llm("Summarise this email thread in 2 sentences, then say who needs to act next. Thread subject: "
                            f"{t['subject']}\n\n{convo}", max_tokens=200).strip()
                    c.execute("INSERT INTO thread_summaries(grant_id,thread_key,msg_count,summary) VALUES(?,?,?,?) "
                              "ON CONFLICT(grant_id,thread_key) DO UPDATE SET msg_count=excluded.msg_count, summary=excluded.summary",
                              (gid, t["key"], len(t["messages"]), s))
                    t["summary"] = s
                    t["summary_source"] = "llm"
                except Exception as e:
                    t["summary_error"] = str(e)
        return {"ok": True, "threads": d["threads"]}


def main():
    ap = argparse.ArgumentParser(description="GrantDesk — zero-dependency grant tracker")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--seed", action="store_true", help="load demo data (replaces existing grants)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--template", metavar="PATH", help="write the blank .xlsx template to PATH and exit")
    args = ap.parse_args()
    init_db()
    if args.template:
        with open(args.template, "wb") as f:
            f.write(template_xlsx())
        print("Template written to", args.template)
        return
    if args.seed:
        print("Seeded", seed(), "demo grants")
    url = f"http://localhost:{args.port}"
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"GrantDesk running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
