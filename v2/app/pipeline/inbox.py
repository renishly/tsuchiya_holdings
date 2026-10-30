"""Student uploads as evidence (Addendum A49, v2.013). Code only; no model.

The student can upload a document to the Inbox of Tsuchibot Main or Tsuchibot C&A on the dashboard (for example a
決算短信 or 業績予想の修正 PDF read on TDnet, which the agent itself may not fetch). At the next run's check-in the
file is stored like a link-checked item: the file and its text under raw/evidence/ with the SHA-256, a focused
excerpt, and an evidence-board item that the debaters may cite. A new upload opens the debate in that same run.

Accepted: PDF with a text layer, HTML, plain text (.txt, .md). Anything else stays in the Inbox (reported once).
The optional note may give the document's date (2026-10-29) and a ticker (1419); otherwise the date is read from
the document's first page, else the upload date, and the title is the note or the file name.
"""
from __future__ import annotations

import re
from pathlib import Path

import evidence
import linkcheck
from lib import flag_injection, iso, log, sha256, write_bytes, write_text

SOURCE = "Student upload (Inbox)"
TEXT_EXT = (".txt", ".md")


def _date(note: str, text: str, uploaded: str) -> str:
    m = re.search(r"(20\d\d)-(\d\d)-(\d\d)", note or "")
    if m:
        return m.group(0)
    m = re.search(r"(20\d\d)年\s*(\d{1,2})月\s*(\d{1,2})日", text[:3000].translate(linkcheck._ZEN))
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return (uploaded or iso())[:10]


def store(st, item: dict, path: str, report=print) -> bool:
    """The dashboard's on_file handler. True = stored (the dashboard deletes its copy); False = keep it there."""
    name = Path(item.get("filename") or "upload").name
    body = Path(path).read_bytes()
    seen = st.setdefault("documents", {}).setdefault("inbox_rejected", [])
    if body[:4] == b"%PDF":
        kind, (text, pages) = "pdf", linkcheck._pdf_text(body)
    elif b"<html" in body[:4000].lower() or b"<!doctype" in body[:300].lower():
        kind, text, pages = "html", linkcheck._html_text(body), 1
    elif name.lower().endswith(TEXT_EXT):
        kind, text, pages = "txt", body.decode("utf-8", errors="replace"), 1
    else:
        kind, text, pages = None, "", 0
    if not kind or not text.strip():
        if item.get("id") not in seen:   # report once; the file stays in the Inbox for the student to delete or replace
            seen.append(item.get("id"))
            report(f"Inbox: {name} not used as evidence ({'no text layer' if kind else 'not a PDF, HTML or text file'}); "
                   "it stays in the Inbox", "error")
        return False
    h = sha256(body)
    i = "E-" + sha256(f"inbox:{h}".encode())[:8]
    b = evidence.load_board()
    if i in b["items"]:
        report(f"Inbox: {name} is already on the evidence board as {i}", "log")
        return True
    injected = flag_injection(st, f"inbox:{name}", text)
    write_bytes(f"raw/evidence/{i}.{kind}", body)
    write_text(f"raw/evidence/{i}.txt", text)
    note = (item.get("note") or "").strip()
    undated = re.sub(r"\b20\d\d-\d\d-\d\d\b", " ", note)
    m = re.search(r"(?<![\d-])(\d{3}[0-9A-Z])(?![\d-])", undated) or re.search(r"(?<!\d)(\d{4})(?!\d)", Path(name).stem)
    ticker = m.group(1) if m else ""
    title = re.sub(r"(?<![\d-])\d{3}[0-9A-Z](?![\d-])", " ", undated)
    title = " ".join(title.split()).strip(" ,;:-") or Path(name).stem
    b["items"][i] = {
        "id": i, "date": _date(note, text, item.get("uploaded_at") or ""), "ticker": ticker, "title": title[:200],
        "url": f"inbox:{name}", "source": SOURCE, "topic": evidence._topic(title), "first_seen": iso(),
        "excerpt": " ".join(text[:1500].split()), "excerpt_long": linkcheck.focused_excerpt(text),
        "link": {"resolved": True, "type": kind, "pages": pages, "chars": len(text), "sha256": h, "checked": iso(),
                 "injection_flagged": bool(injected), "uploaded_by": "student"}}
    evidence.save_board(b)
    log(st, "inbox", {"file": name, "id": i}, f"stored ({kind}, {pages} page(s), {len(text)} chars)")
    report(f"Inbox: stored {name} as evidence {i}", "log")
    return True
