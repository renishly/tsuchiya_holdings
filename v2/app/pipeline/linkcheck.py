"""Link check and claim check for the Md debate (Addendum A43, v2.007). Deterministic code; no model.

Link check (before the debaters start), for each flagged evidence-board item:
  1. resolve the link through the guarded fetch: allowlisted domain, robots.txt, HTTP 200 (EDINET filings through
     EDINET's REST API, v2 documents endpoint, as PDF); the file must be what it claims to be (a PDF starts with
     %PDF, an HTML page with markup);
  2. save a snapshot (raw/evidence/<id>.<ext>) with its SHA-256, and the extracted text (raw/evidence/<id>.txt);
  3. check the text for injected instructions (as every fetch is);
  4. build a focused excerpt (at most 3,000 characters): the first page, then the passages around forecast,
     revision and order terms and the forecast table.
  An item whose link does not resolve is marked unresolved: the debaters cannot cite it.
Claim check (after each debate round): every number in a claim must appear in the text of an item that claim
  cites (as written, or equal within 1% after removing commas and units). Claims with unverified numbers are
  flagged for C&A and counted; code does not trust the model's figures.
"""
from __future__ import annotations

import io
import json
import re

import evidence
from lib import ROOT, BudgetStop, FetchError, GuardrailError, fetch, flag_injection, iso, log, run_ctx, sha256, write_bytes, write_text

EXCERPT_CHARS = 3000
KEYWORDS = ["業績予想", "修正", "予想", "受注", "引渡", "特別損失", "特別利益", "減損", "営業利益", "経常利益", "売上高",
            "見込", "遅延", "延期", "災害", "訴訟", "通期"]
_ZEN = str.maketrans("０１２３４５６７８９，．△－", "0123456789,.--")
_NUM = re.compile(r"[-−]?\d[\d,]*(?:\.\d+)?")


def _pdf_text(body: bytes, pages: int = 30) -> tuple[str, int]:
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(body)) as d:
            return "\n".join((p.extract_text() or "") for p in d.pages[:pages]), len(d.pages)
    except Exception:
        return "", 0


def _html_text(body: bytes) -> str:
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(body, "lxml").get_text("\n", strip=True)
    except Exception:
        return ""


def focused_excerpt(text: str) -> str:
    """First page (up to 900 chars), then windows around key terms, de-duplicated, up to EXCERPT_CHARS."""
    t = re.sub(r"[ \t]+", " ", text)
    parts, used = [t[:900]], [(0, 900)]
    for kw in KEYWORDS:
        for m in re.finditer(kw, t):
            a, b = max(0, m.start() - 220), min(len(t), m.end() + 280)
            if any(a < u2 and b > u1 for u1, u2 in used):
                continue
            used.append((a, b))
            parts.append("… " + t[a:b] + " …")
            if sum(len(p) for p in parts) >= EXCERPT_CHARS:
                return "\n".join(parts)[:EXCERPT_CHARS]
    return "\n".join(parts)[:EXCERPT_CHARS]


def _resolve(st, item: dict) -> dict:
    url = item["url"]
    m = re.search(r"WZEK0040\.aspx\?(S1[0-9A-Z]{6})", url)
    try:
        if m:  # EDINET filing: the REST API's PDF of the document
            import edinet
            p = edinet.download(st, m.group(1), 2)
            body, kind = p.read_bytes(), "pdf"
        else:
            body, meta = fetch(st, url, binary=True, snapshot=False, purpose="link check: evidence item")
            kind = "pdf" if body[:4] == b"%PDF" else "html" if b"<html" in body[:2000].lower() or b"<!doctype" in body[:200].lower() else "other"
    except (FetchError, GuardrailError, BudgetStop) as e:
        return {"resolved": False, "error": str(e)[:200], "checked": iso()}
    if kind == "other":
        return {"resolved": False, "error": "not a PDF or HTML page", "checked": iso()}
    if url.lower().split("?")[0].endswith(".pdf") and kind != "pdf":
        return {"resolved": False, "error": "link ends in .pdf but did not return a PDF", "checked": iso()}
    text, pages = _pdf_text(body) if kind == "pdf" else (_html_text(body), 1)
    if not text.strip():
        return {"resolved": False, "error": "no extractable text", "checked": iso()}
    injected = flag_injection(st, url, text)
    h = sha256(body)
    write_bytes(f"raw/evidence/{item['id']}.{kind}", body)
    write_text(f"raw/evidence/{item['id']}.txt", text)
    return {"resolved": True, "type": kind, "pages": pages, "chars": len(text), "sha256": h, "checked": iso(),
            "injection_flagged": bool(injected)}


def check(st, ids: list[str]) -> dict:
    """Resolve each item's link once (cached in the board); return the link report and the focused excerpts."""
    b = evidence.load_board()
    test = run_ctx(st).get("mode") == "test"
    report, items = [], []
    for i in ids:
        it = b["items"].get(i)
        if not it:
            report.append({"id": i, "resolved": False, "error": "not on the evidence board"})
            continue
        if it.get("context_only"):   # A48: GDELT headlines point to outside sites the agent does not fetch
            it["link"] = {"resolved": False, "error": "context only (headline from GDELT): not citable", "checked": iso()}
        elif not (it.get("link") or {}).get("resolved") and not test:
            it["link"] = _resolve(st, it)
            if it["link"]["resolved"]:
                txt = (ROOT / f"raw/evidence/{i}.txt").read_text(encoding="utf-8")
                it["excerpt_long"] = focused_excerpt(txt)
        lk = it.get("link") or {"resolved": False, "error": "not checked (test mode)" if test else "not checked"}
        report.append({"id": i, "title": it["title"][:120], "url": it["url"], **lk})
        if lk.get("resolved"):
            items.append({k: it.get(k) for k in ("id", "date", "ticker", "source", "topic", "title", "url")}
                         | {"excerpt": it.get("excerpt_long") or it.get("excerpt", ""), "pages": lk.get("pages"),
                            "sha256": lk.get("sha256")})
    evidence.save_board(b)
    log(st, "linkcheck", {"ids": len(ids)}, f"{sum(1 for r in report if r.get('resolved'))} resolved")
    return {"report": report, "items": items, "resolved_ids": [r["id"] for r in report if r.get("resolved")]}


# ---------------------------------------------------------------- claim check
def _numbers(s: str) -> list[float]:
    out = []
    for tok in _NUM.findall(s.translate(_ZEN)):
        try:
            out.append(float(tok.replace(",", "").replace("−", "-")))
        except ValueError:
            continue
    return out


def check_claims(positions: dict) -> dict:
    """Every number in a claim must appear in the text of an item it cites. Annotates the positions in place."""
    texts: dict[str, list[float]] = {}
    raw: dict[str, str] = {}
    def nums_for(i):
        if i not in texts:
            p = ROOT / f"raw/evidence/{i}.txt"
            raw[i] = p.read_text(encoding="utf-8").translate(_ZEN) if p.exists() else ""
            texts[i] = _numbers(raw[i])
        return texts[i]
    summary = {}
    for name, pos in positions.items():
        ok = bad = 0
        for c in pos.get("claims", []):
            cited = [n for i in c.get("evidence", []) for n in nums_for(i)]
            body = " ".join(raw.get(i, "") for i in c.get("evidence", []))
            unver = []
            for tok in _NUM.findall(str(c.get("text", "")).translate(_ZEN)):
                x = float(tok.replace(",", "").replace("−", "-"))
                if abs(x) <= 12 and x == int(x):      # small counts and months ("3 peers", "Q3")
                    continue
                if 1990 <= x <= 2035:                 # years
                    continue
                if tok in body or any(abs(x - n) <= 0.01 * abs(n) for n in cited if n):
                    continue
                unver.append(tok)
            c["unverified_numbers"] = unver
            if unver:
                bad += 1
            else:
                ok += 1
        pos["claims_verified"], pos["claims_with_unverified_numbers"] = ok, bad
        summary[name] = {"verified": ok, "with_unverified_numbers": bad}
    return summary
