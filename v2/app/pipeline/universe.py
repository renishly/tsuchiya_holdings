"""M1 universe (Addendum A39, v2.006): listed Japanese housebuilders, their annual-report history and the screen.

Candidates: listed companies in TSE 建設業 / 不動産業 whose core business is building or selling houses, checked
against EDINET's code list (listed, with an EDINET code). The proposal's screen (D1) is applied to each candidate's
latest 有報: the residential share of segment revenue must be at least 50% (segments matched by name); a company
reporting a single segment passes with a note. Delisted housebuilders (e.g. ヒノキヤ, 三井ホーム, ウッドフレンズ)
cannot be included because no price history is available for them (survivorship bias, stated in every report).

History: two annual reports per company, the latest and the one five years earlier. Each 有報's five-year summary
(主要な経営指標等の推移) gives revenue, ordinary profit, net income, net assets, total assets, BPS, EPS and ROE for
five fiscal years, so two filings cover about ten. Point in time: a value is treated as public from the filing date
of the report that first contained it (the current-year column), or from the statutory deadline (fiscal year end +
3 months) for earlier columns whose first filing was not downloaded.

Discovery is resumable: EDINET's daily document list is scanned once per date for all candidates at the same time.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re

import edinet
from lib import ROOT, BudgetStop, FetchError, GuardrailError, iso, log, write_text

# ticker -> (EDINET code, name, fiscal-year-end month). Checked against EDINET's code list on 23 Oct 2026 (listed).
CANDIDATES = {
    "1840": ("E00240", "土屋ホールディングス", 10), "1419": ("E27305", "タマホーム", 5),
    "1873": ("E00197", "日本ハウスホールディングス", 4), "8999": ("E04044", "グランディハウス", 3),
    "1928": ("E00143", "積水ハウス", 1), "1925": ("E00048", "大和ハウス工業", 3), "1911": ("E00011", "住友林業", 12),
    "3291": ("E27759", "飯田グループホールディングス", 3), "3288": ("E27842", "オープンハウスグループ", 9),
    "3465": ("E31979", "ケイアイスター不動産", 3), "8860": ("E03953", "フジ住宅", 3), "8917": ("E04013", "ファースト住建", 10),
    "8996": ("E04048", "ハウスフリーダム", 12), "1420": ("E27273", "サンヨーホームズ", 3), "8904": ("E00311", "AVANTIA", 8),
    "2974": ("E34944", "大英産業", 9), "2983": ("E35144", "アールプランナー", 1), "1431": ("E31614", "Lib Work", 6),
    "3280": ("E26966", "エストラスト", 2), "3293": ("E30066", "アズマハウス", 3), "2999": ("E35234", "ホームポジション", 8),
    "1878": ("E00218", "大東建託", 3),
}
RESIDENTIAL = re.compile(r"House|Housing|Home|Residential|Detached|Dwelling|Condominium|Remodel|Renovation|Reform|"
                         r"RealEstateSales|RealEstateBusiness(?!.*Leas)|Construction(?=.*Hous)", re.I)
# Segments whose English member name does not show that they are residential (or wrongly looks residential).
# Each entry is a judgment from the company's own segment description in its 有報; listed in the research report.
SEGMENT_OVERRIDES = {
    "1928": {"OverseasReportableSegmentMember": (True, "積水ハウス 国際事業: mainly detached and rental housing in the US and Australia"),
             "DevelopmentReportableSegmentMember": (False, "開発型ビジネス: offices, hotels, logistics and condominium development (mixed)")},
    "1911": {"ConstructionAndRealEstateDivisionReportableSegmentsMember": (True, "住友林業 建築・不動産事業: mainly US/Australian housing"),
             "HousingBusinessReportableSegmentsMember": (True, "住宅事業: domestic custom-built houses")},
    "1878": {"ConstructionReportableSegmentsMember": (True, "大東建託 建設事業: construction of rental apartment buildings for landowners")},
    "3280": {"RealEstateSaleInLotsBusinessReportableSegmentsMember": (True, "エストラスト 不動産分譲事業: detached houses and condominiums for sale")},
    "3293": {"RealEstateAndConstructionReportableSegmentsMember": (True, "アズマハウス 不動産事業・建設事業: built-for-sale and custom houses")},
}
FUND_FILE = "data/universe_fundamentals.csv"
FUND_COLS = ["ticker", "fy_end", "item", "value", "published", "doc_id", "basis"]
SUMMARY = {  # item -> element names (5-year summary, consolidated)
    "revenue": ["NetSalesSummaryOfBusinessResults", "RevenueSummaryOfBusinessResults", "OperatingRevenue1SummaryOfBusinessResults",
                "RevenuesSummaryOfBusinessResults", "NetSalesOfCompletedConstructionContractsSummaryOfBusinessResults"],
    "ordinary_profit": ["OrdinaryIncomeLossSummaryOfBusinessResults"],
    "net_income": ["ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults"],
    "net_assets": ["NetAssetsSummaryOfBusinessResults"],
    "total_assets": ["TotalAssetsSummaryOfBusinessResults"],
    "bps": ["NetAssetsPerShareSummaryOfBusinessResults"],
    "eps": ["BasicEarningsLossPerShareSummaryOfBusinessResults"],
    "roe": ["RateOfReturnOnEquitySummaryOfBusinessResults"],
}
CTX = {"CurrentYear": 0, "Prior1Year": 1, "Prior2Year": 2, "Prior3Year": 3, "Prior4Year": 4, "Prior5Year": 5}


def _month_end(y: int, m: int) -> dt.date:
    return dt.date(y + m // 12, m % 12 + 1, 1) - dt.timedelta(days=1)


def latest_fy_end(today: dt.date, month: int) -> dt.date:
    """Most recent fiscal-year end whose annual report deadline (3 months + a week) has passed."""
    for y in (today.year, today.year - 1, today.year - 2):
        e = _month_end(y, month)
        if e + dt.timedelta(days=98) <= today:
            return e
    return _month_end(today.year - 2, month)


def _labels(last: dt.date, m: int) -> list[tuple[str, dt.date]]:
    """Filings to collect: the latest, every other year back eight years (statements carry the prior year too, so
    this covers every year for EV/EBITDA), and the one five years back (its summary reaches ten years)."""
    out = [("latest", last)] + [(f"minus{k}", _month_end(last.year - k, m)) for k in (2, 4, 6, 8)]
    return out + [("minus5", _month_end(last.year - 5, m))]


def _state(st) -> dict:
    return st["documents"].setdefault("universe", {"scanned": {}, "docs": {}, "screen": {}})


# ---------------------------------------------------------------- discovery and download
def discover(st, max_days: int = 400) -> dict:
    """Find the latest and the five-years-earlier 有報 for every candidate (resumable; one list call per date)."""
    u = _state(st)
    today = dt.date.fromisoformat(iso()[:10])
    by_code = {v[0]: t for t, v in CANDIDATES.items()}
    targets = {}
    for t, (code, name, m) in CANDIDATES.items():
        last = latest_fy_end(today, m)
        for label, end in _labels(last, m):
            if not u["docs"].get(t, {}).get(label):
                targets[(t, label)] = end.isoformat()
    calls = 0
    for (t, label), end in sorted(targets.items(), key=lambda kv: kv[1]):
        if u["docs"].get(t, {}).get(label):
            continue
        for date in edinet.candidate_dates(end):
            if date in u["scanned"]:
                continue
            if calls >= max_days:
                return {"calls": calls, "remaining": sum(1 for k in targets if not u["docs"].get(k[0], {}).get(k[1]))}
            try:
                res = edinet.list_day(st, date)
            except (FetchError, BudgetStop) as e:
                log(st, "universe", {"date": date}, f"paused: {e}")
                return {"calls": calls, "paused": str(e)[:160]}
            calls += 1
            u["scanned"][date] = True
            for r in res:  # record every candidate's annual report seen that day
                tk = by_code.get(r.get("edinetCode"))
                if not tk or r.get("docTypeCode") != "120" or r.get("withdrawalStatus") not in (None, "0"):
                    continue
                m = CANDIDATES[tk][2]
                last = latest_fy_end(today, m)
                for lab, e in _labels(last, m):
                    if r.get("periodEnd") == e.isoformat():
                        u["docs"].setdefault(tk, {})[lab] = {k: r.get(k) for k in ("docID", "periodEnd", "submitDateTime")}
            if u["docs"].get(t, {}).get(label):
                break
    remaining = sum(1 for k in targets if not u["docs"].get(k[0], {}).get(k[1]))
    return {"calls": calls, "remaining": remaining}


def ingest(st, max_downloads: int = 30) -> dict:
    """Download the found filings (cached by docID) and write the five-year summaries to data/universe_fundamentals.csv."""
    u = _state(st)
    rows = {(r["ticker"], r["fy_end"], r["item"]): r for r in read_fundamentals()}
    done = 0
    for t, docs in sorted(u["docs"].items()):
        for label in ("minus8", "minus6", "minus5", "minus4", "minus2", "latest"):  # older first: newer values win
            d = docs.get(label)
            if not d or d.get("ingested") == 2:   # 2 = summary and statements extracted (v2.006)
                continue
            if done >= max_downloads:
                break
            try:
                z = edinet.download(st, d["docID"], 5)
            except (FetchError, GuardrailError, BudgetStop) as e:
                log(st, "universe", {"docID": d["docID"]}, f"download paused: {e}")
                return {"downloaded": done, "paused": str(e)[:160]}
            facts = edinet.read_xbrl_csv(z)
            for row in _summary_rows(t, d, facts) + _statement_rows(t, d, facts):
                k = (row["ticker"], row["fy_end"], row["item"])
                if k not in rows or rows[k]["basis"] != "current-year column" or row["basis"] == "current-year column":
                    rows[k] = row
            if label == "latest":
                u["screen"][t] = _residential_share(facts, t)
            d["ingested"] = 2
            done += 1
    _write_fundamentals(list(rows.values()))
    return {"downloaded": done, "rows": len(rows)}


def _summary_rows(ticker: str, doc: dict, facts: list[dict]) -> list[dict]:
    pe = dt.date.fromisoformat(doc["periodEnd"])
    filed = (doc.get("submitDateTime") or "")[:10]
    out = []
    for item, names in SUMMARY.items():
        got = {}
        for x in facts:
            el = x["element"].split(":")[-1]
            if el not in names or "NonConsolidated" in x["context"]:
                continue
            m = re.match(r"(CurrentYear|Prior\dYear)(Instant|Duration)$", x["context"])
            if not m:
                continue
            n = CTX[m.group(1)]
            v = x["value"].replace(",", "")
            try:
                val = float(v)
            except ValueError:
                continue
            if n not in got or names.index(el) < got[n][1]:
                got[n] = (val, names.index(el))
        for n, (val, _) in got.items():
            fy = _month_end(pe.year - n, pe.month)
            deadline = (fy + dt.timedelta(days=98)).isoformat()
            out.append({"ticker": ticker, "fy_end": fy.isoformat(), "item": item, "value": val,
                        "published": filed if n == 0 and filed else deadline, "doc_id": doc["docID"],
                        "basis": "current-year column" if n == 0 else f"prior-year column ({n})"})
    return out


STATEMENTS = {  # item -> (element names, kind); consolidated statements, current and prior-year columns
    "operating_profit": (["OperatingIncome"], "Duration"),
    "depreciation": (["DepreciationAndAmortizationOpeCF", "DepreciationOpeCF"], "Duration"),
    "cash": (["CashAndCashEquivalents"], "Instant"),
}
DEBT = ["ShortTermLoansPayable", "CurrentPortionOfLongTermLoansPayable", "LongTermLoansPayable", "BondsPayable",
        "CurrentPortionOfBonds", "LeaseObligationsCL", "LeaseObligationsNCL", "CommercialPapersLiabilities"]


def _statement_rows(ticker: str, doc: dict, facts: list[dict]) -> list[dict]:
    """Operating profit, depreciation, cash and interest-bearing debt for the filing's year and the prior year."""
    pe = dt.date.fromisoformat(doc["periodEnd"])
    filed = (doc.get("submitDateTime") or "")[:10]
    val = {}
    for x in facts:
        m = re.match(r"(CurrentYear|Prior1Year)(Instant|Duration)$", x["context"])
        if not m:
            continue
        try:
            v = float(x["value"].replace(",", ""))
        except ValueError:
            continue
        val[(x["element"].split(":")[-1], CTX[m.group(1)], m.group(2))] = v
    out = []
    for n in (0, 1):
        fy = _month_end(pe.year - n, pe.month)
        pub = filed if n == 0 and filed else (fy + dt.timedelta(days=98)).isoformat()
        basis = "current-year column" if n == 0 else "prior-year column (1)"
        for item, (names, kind) in STATEMENTS.items():
            v = next((val[(e, n, kind)] for e in names if (e, n, kind) in val), None)
            if v is not None:
                out.append({"ticker": ticker, "fy_end": fy.isoformat(), "item": item, "value": v, "published": pub,
                            "doc_id": doc["docID"], "basis": basis})
        parts = [val[(e, n, "Instant")] for e in DEBT if (e, n, "Instant") in val]
        if parts or ("CashAndCashEquivalents", n, "Instant") in val:  # no borrowings tagged: debt is zero
            out.append({"ticker": ticker, "fy_end": fy.isoformat(), "item": "debt", "value": sum(parts), "published": pub,
                        "doc_id": doc["docID"], "basis": basis})
    return out


def rescreen(st) -> None:
    """Recompute the screen from the cached latest filings (after a change to SEGMENT_OVERRIDES; no network)."""
    u = _state(st)
    for t, docs in u["docs"].items():
        d = docs.get("latest")
        z = ROOT / "raw" / "edinet" / d["docID"] / f"{d['docID']}_type5.csv.zip" if d else None
        if z and z.exists():
            u["screen"][t] = _residential_share(edinet.read_xbrl_csv(z), t)


def _residential_share(facts: list[dict], ticker: str = "") -> dict:
    seg = {}
    for x in facts:
        if x["element"].split(":")[-1] != "RevenuesFromExternalCustomers" or not x["context"].startswith("CurrentYearDuration_"):
            continue
        member = x["context"].split("_", 1)[1]
        if not re.search(r"ReportableSegments?Member$", member) or member in ("ReportableSegmentsMember", "ReportableSegmentMember"):
            continue
        name = re.sub(r"^.*?-\d{3}", "", member)
        try:
            seg[name] = float(x["value"])
        except ValueError:
            continue
    if not seg:
        return {"share": None, "segments": {}, "pass": True, "note": "single segment or no segment revenue tagged: passes"}
    total = sum(seg.values())
    ov = SEGMENT_OVERRIDES.get(ticker, {})
    is_res = {k: (ov[k][0] if k in ov else bool(RESIDENTIAL.search(k))) for k in seg}
    res = sum(v for k, v in seg.items() if is_res[k])
    share = res / total if total else None
    return {"share": round(share, 3) if share is not None else None, "pass": share is not None and share >= 0.5,
            "segments": {k: round(v / total, 3) for k, v in seg.items()}, "residential": sorted(k for k in seg if is_res[k]),
            "note": "; ".join(f"{k}: {ov[k][1]}" for k in seg if k in ov)}


def members(st) -> list[str]:
    """Tickers in the universe: candidates whose latest 有報 passed the residential screen (Tsuchiya always)."""
    scr = _state(st)["screen"]
    return [t for t in CANDIDATES if t == "1840" or scr.get(t, {}).get("pass")]


# ---------------------------------------------------------------- the fundamentals file
def read_fundamentals() -> list[dict]:
    p = ROOT / FUND_FILE
    if not p.exists():
        return []
    with open(p, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["value"] = float(r["value"])
    return rows


def _write_fundamentals(rows: list[dict]):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FUND_COLS)
    w.writeheader()
    for r in sorted(rows, key=lambda r: (r["ticker"], r["fy_end"], r["item"])):
        w.writerow({k: r[k] for k in FUND_COLS})
    write_text(FUND_FILE, buf.getvalue())


def refresh(st, max_days: int = 400, max_downloads: int = 30) -> dict:
    """One research-run step: discover, download, screen. Resumable across runs."""
    d = discover(st, max_days)
    i = ingest(st, max_downloads)
    rescreen(st)
    u = _state(st)
    return {"discovery": d, "ingest": i, "members": members(st),
            "screen": {t: u["screen"].get(t, {}).get("share") for t in CANDIDATES}}
