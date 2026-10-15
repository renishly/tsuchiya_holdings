"""Google Sheets mirror (phase P5 and every-run sync). The CSVs are the system of record.

- One-time approval (kind sheets_first_write) before the first write; tab layout changes need a new approval.
- Before writing a tab, rows the student marked student-verified / student-corrected in the Sheet are pulled
  back into the CSV (they become locked), so the Sheet never overwrites student work.
- value cells are written as =HYPERLINK(source_url, value); the raw source_url column is kept.
- One batch update per tab per run; backs off on HTTP 429. Never shares, never deletes tabs or the Sheet.
"""
from __future__ import annotations

import json
import os
import time

from lib import (CsvStore, LOCKED, ROOT, SCHEMAS, approved, consume, iso, log, queue_approval, run_ctx,
                 write_text)

TABS = list(SCHEMAS) + ["Agent Warnings"]
WARN_COLS = ["warning_id", "raised_jst", "kind", "detail", "csv", "record_id", "source_url", "status"]

def layout() -> dict:
    return {t: (SCHEMAS[t] if t in SCHEMAS else WARN_COLS) for t in TABS}

def _q(s: str) -> str:
    return str(s).replace('"', '""')

def cell_value(row: dict) -> str:
    v, u = row.get("value", ""), row.get("source_url", "")
    if v == "" or not u.startswith("http"):
        return v
    num = v.replace(".", "", 1).replace("-", "", 1).isdigit()
    inner = v if num else f'"{_q(v)}"'
    return f'=HYPERLINK("{_q(u)}",{inner})'

def warnings_rows(stores: dict[str, CsvStore], extra: list[dict]) -> list[list[str]]:
    out = []
    n = 0
    for name, st in stores.items():
        for r in st.rows.values():
            if r["validation_status"].startswith("failed:") or r["agent_status"] == "unresolved":
                n += 1
                kind = r["validation_status"] if r["validation_status"].startswith("failed:") else "unresolved"
                out.append([f"W{n:05d}", r["access_date_jst"], kind, r["correction_note"][:300], name, r["record_id"],
                            r["source_url"], "open"])
    for e in extra:
        n += 1
        out.append([f"W{n:05d}", iso(), e.get("kind", "notice"), e.get("detail", "")[:300], e.get("csv", ""), "",
                    e.get("source_url", ""), "open"])
    return out

def write_warnings_csv(rows):
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(WARN_COLS)
    w.writerows(rows)
    write_text("state/agent_warnings.csv", "﻿" + buf.getvalue())

def sync(st, stores: dict[str, CsvStore], extra_warnings: list[dict], test: bool = False) -> dict:
    warn = warnings_rows(stores, extra_warnings)
    write_warnings_csv(warn)
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "")
    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    params = {"sheet": "GOOGLE_SHEET_ID (env)", "layout": {k: len(v) for k, v in layout().items()}}
    if "sheets_first_write" not in st["approved"]:
        ar = approved(st, "sheets_first_write", params)
        if not ar:
            ar = queue_approval(st, "sheets_first_write", "First write to the Google Sheet (create/refresh tabs)",
                                "Sheet in env GOOGLE_SHEET_ID", params,
                                "One-time approval of the Sheet and tab layout: " + ", ".join(TABS))
            return {"synced": False, "pending": ar, "warnings_csv": "state/agent_warnings.csv"}
        consume(st, ar)
        st["approved"]["sheets_first_write"] = {"id": ar, "layout": params["layout"], "at": iso()}
    elif st["approved"]["sheets_first_write"]["layout"] != params["layout"]:
        ar = approved(st, "sheets_layout_change", params)
        if not ar:
            ar = queue_approval(st, "sheets_layout_change", "Change Sheet tab/column layout", "Sheet", params,
                                "CSV schema or tab list differs from the approved layout")
            return {"synced": False, "pending": ar}
        consume(st, ar)
        st["approved"]["sheets_first_write"]["layout"] = params["layout"]
    if test or run_ctx(st).get("mode") == "test":
        payload = {t: _matrix(stores.get(t), warn if t == "Agent Warnings" else None) for t in TABS}
        write_text("tmp/sheets_dryrun.json", json.dumps({k: v[:5] for k, v in payload.items()}, ensure_ascii=False, indent=1))
        log(st, "sheets_sync", {"mode": "test"}, "dry run written to tmp/sheets_dryrun.json")
        return {"synced": False, "dry_run": True}
    if not sheet_id or not creds:
        log(st, "sheets_sync", {}, "Sheets auth failed: GOOGLE_SHEET_ID or GOOGLE_APPLICATION_CREDENTIALS not set")
        return {"synced": False, "error": "Sheets auth failed (env not set)"}
    try:
        import gspread
    except ImportError:
        log(st, "sheets_sync", {}, "Sheets library missing (gspread not installed in this environment)")
        return {"synced": False, "error": "Sheets library missing: gspread not installed (environment setup script / network)"}
    try:
        gc = gspread.service_account(filename=creds) if os.path.exists(creds) else \
            gspread.service_account_from_dict(json.loads(creds))
        sh = gc.open_by_key(sheet_id)
    except Exception as e:  # auth/permission errors: never retry with other credentials
        log(st, "sheets_sync", {}, f"Sheets auth failed: {type(e).__name__}")
        return {"synced": False, "error": f"Sheets auth failed ({type(e).__name__})"}
    existing = {ws.title: ws for ws in sh.worksheets()}
    pulled = 0
    res = {"synced": True, "tabs": {}}
    for t in TABS:
        ws = existing.get(t) or sh.add_worksheet(title=t, rows=100, cols=len(layout()[t]))
        if t in stores:
            pulled += _pull_student_edits(st, ws, stores[t])
        mat = _matrix(stores.get(t), warn if t == "Agent Warnings" else None)
        for attempt in range(4):
            try:
                ws.resize(rows=max(len(mat), 2), cols=len(mat[0]))
                ws.update(range_name="A1", values=mat, value_input_option="USER_ENTERED")
                break
            except Exception as e:
                if "429" in str(e) and attempt < 3:
                    time.sleep(30 * (attempt + 1))
                    continue
                log(st, "sheets_sync", {"tab": t}, f"error {type(e).__name__}")
                res["synced"] = False
                break
        res["tabs"][t] = len(mat) - 1
    res["student_rows_pulled"] = pulled
    log(st, "sheets_sync", {"tabs": list(res["tabs"])}, res)
    return res

def _matrix(store: CsvStore | None, warn=None):
    if warn is not None:
        return [WARN_COLS] + warn
    cols = store.cols
    rows = [cols]
    for rid in store.order:
        r = store.rows[rid]
        rows.append([cell_value(r) if c == "value" else r[c] for c in cols])
    return rows

def _pull_student_edits(st, ws, store: CsvStore) -> int:
    """Copy student-verified/corrected rows from the Sheet into the CSV (locked thereafter)."""
    try:
        vals = ws.get_all_values(value_render_option="FORMATTED_VALUE")
    except Exception:
        return 0
    if not vals or vals[0] != store.cols:
        return 0
    n = 0
    ix = {c: i for i, c in enumerate(vals[0])}
    for line in vals[1:]:
        if len(line) < len(vals[0]):
            continue
        status = line[ix["agent_status"]]
        rid = line[ix["record_id"]]
        if status in LOCKED and rid in store.rows and store.rows[rid]["agent_status"] not in LOCKED:
            r = store.rows[rid]
            r["agent_status"] = status
            r["value"] = line[ix["value"]]
            r["correction_note"] = line[ix["correction_note"]]
            store.changed.append(rid)
            n += 1
            log(st, "student_correction", {"record_id": rid, "csv": store.name}, status)
    return n
