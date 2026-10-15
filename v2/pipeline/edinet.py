"""EDINET API v2 discovery, download and XBRL extraction (phases P1, P2; peer inputs for P6).

API: EDINET API v2 (仕様書 Version 2, October 2025 edition, ESE140206).
  documents.json?date=YYYY-MM-DD&type=2  -> list of filings that day
  documents/{docID}?type=1 ZIP (XBRL + iXBRL HTML) | 2 PDF | 5 ZIP of XBRL_TO_CSV (UTF-16, tab)
The key comes only from env EDINET_API_KEY and is never written to disk or logs.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import re
import zipfile
from pathlib import Path

from lib import (CONFIG, ROOT, BudgetStop, CsvStore, FetchError, GuardrailError, base_row,
                 fetch, iso, log, out, record_id, run_ctx, safe_path, sha256, tick, unresolved,
                 write_bytes, write_text)

API = "https://api.edinet-fsa.go.jp/api/v2"
DOC_TYPES = {"120": "有価証券報告書", "130": "訂正有価証券報告書"}
PEER_EDINET = {"1873": "E00197", "1419": "E27305", "8999": "E04044", "1928": "E00143"}  # Addendum A30

def _key():
    k = os.environ.get("EDINET_API_KEY", "")
    return {"Subscription-Key": k} if k else None

def doc_url(doc_id: str) -> str:
    """Stable public viewer URL for a filing (used as source_url)."""
    return f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{doc_id}"

def pdf_url(doc_id: str) -> str:
    return f"https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/{doc_id}.pdf"

# ---------------------------------------------------------------- discovery (P1)
def candidate_dates(fy_end: str, months_after: int = 3) -> list[str]:
    """Weekdays where an annual report for this FY end is most likely filed (statutory deadline 3 months)."""
    end = dt.date.fromisoformat(fy_end)
    y, m = end.year + (end.month + months_after - 1) // 12, (end.month + months_after - 1) % 12 + 1
    deadline = (dt.date(y, m, 1) + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
    near = [deadline - dt.timedelta(days=i) for i in range(0, 12)]
    early = [deadline - dt.timedelta(days=i) for i in range(12, 40)]
    late = [deadline + dt.timedelta(days=i) for i in range(1, 15)]
    return [d.isoformat() for d in near + early + late if d.weekday() < 5]

def list_day(st, date: str) -> list[dict]:
    if _key() is None and run_ctx(st).get("mode") != "test":
        raise FetchError("no_key", "EDINET_API_KEY is not set")
    body, meta = fetch(st, f"{API}/documents.json", params={"date": date, "type": 2},
                       purpose="EDINET document list", snapshot=False, secret_params=_key())
    data = json.loads(body)
    if str(data.get("metadata", {}).get("status")) not in ("200", "None") and "results" not in data:
        raise FetchError(data.get("metadata", {}).get("status"), data.get("metadata", {}).get("message"))
    return data.get("results") or []

def discover(st: dict, edinet_code: str, fy_ends: dict[str, str], key: str) -> dict:
    """Resume-able search for annual reports (and corrections) for each fiscal year.

    Cursor per company in state['documents'][key] = {"fy": {...}, "scanned": {date: True}}.
    """
    docs = st["documents"].setdefault(key, {"fy": {}, "scanned": {}, "corrections": []})
    for term, fy_end in fy_ends.items():
        fyd = docs["fy"].setdefault(term, {"period_end": fy_end, "asr": None, "searched_dates": []})
        if fyd["asr"]:
            continue
        for date in candidate_dates(fy_end):
            if date in docs["scanned"]:
                continue
            res = list_day(st, date)
            docs["scanned"][date] = True
            fyd["searched_dates"].append(date)
            for r in res:
                if r.get("edinetCode") != edinet_code or r.get("docTypeCode") not in DOC_TYPES:
                    continue
                if r.get("withdrawalStatus") not in (None, "0"):
                    continue
                rec = {k: r.get(k) for k in ("docID", "docTypeCode", "periodStart", "periodEnd", "docDescription",
                                              "submitDateTime", "xbrlFlag", "pdfFlag", "csvFlag", "parentDocID")}
                if r["docTypeCode"] == "120":
                    t = _term_for(fy_ends, r.get("periodEnd"))
                    if t:
                        docs["fy"][t]["asr"] = rec
                elif rec not in docs["corrections"]:
                    docs["corrections"].append(rec)
                log(st, "edinet_discover", {"date": date, "docID": r["docID"]}, DOC_TYPES[r["docTypeCode"]])
            if fyd["asr"]:
                break
    return docs

def _term_for(fy_ends, period_end):
    for t, e in fy_ends.items():
        if e == period_end:
            return t
    return None

def scan_recent(st: dict, days: int = 1) -> list[dict]:
    """Daily steady-state check for new filings by Tsuchiya (any doc type) — returns new docs."""
    found = []
    today = dt.date.fromisoformat(iso()[:10])
    for i in range(days, -1, -1):
        d = (today - dt.timedelta(days=i)).isoformat()
        for r in list_day(st, d):
            if r.get("edinetCode") == CONFIG["edinet_code"]:
                found.append({k: r.get(k) for k in ("docID", "docTypeCode", "periodEnd", "docDescription", "submitDateTime")})
    return found

# ---------------------------------------------------------------- download (cached by docID)
def download(st: dict, doc_id: str, typ: int) -> Path:
    if not re.fullmatch(r"S1[0-9A-Z]{6}", doc_id):
        raise GuardrailError(f"bad docID {doc_id!r}")
    ext = {1: "zip", 2: "pdf", 5: "csv.zip"}[typ]
    rel = Path(f"raw/edinet/{doc_id}/{doc_id}_type{typ}.{ext}")
    p = ROOT / rel
    if p.exists():
        return p  # never re-download a cached docID
    if _key() is None and run_ctx(st).get("mode") != "test":
        raise FetchError("no_key", "EDINET_API_KEY is not set")
    body, meta = fetch(st, f"{API}/documents/{doc_id}", params={"type": typ}, binary=True,
                       purpose=f"EDINET download type {typ}", secret_params=_key())
    if body[:1] == b"{":  # API returns JSON on error
        raise FetchError("api", body[:200].decode("utf-8", "replace"))
    write_bytes(rel, body)
    write_text(f"raw/edinet/{doc_id}/{doc_id}_type{typ}.sha256", sha256(body) + "\n")
    return p

# ---------------------------------------------------------------- XBRL_TO_CSV parsing
def read_xbrl_csv(zip_path: Path) -> list[dict]:
    rows = []
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if not name.endswith(".csv") or "XBRL_TO_CSV" not in name:
                continue
            if "-asr-" not in name and "jpcrp" not in name:
                continue  # skip audit-report CSVs (jpaud)
            raw = z.read(name)
            text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
            rd = csv.reader(io.StringIO(text), delimiter="\t")
            header = next(rd)
            for r in rd:
                if len(r) < 9:
                    continue
                rows.append(dict(zip(["element", "label", "context", "rel_year", "cons", "period", "unit_id", "unit", "value"], r[:9])))
    return rows

# Line items: (statement, en, ja, [element candidates], unit, context kind, fallback label regex)
D, I = "Duration", "Instant"
LINE_ITEMS = [
    ("PL", "Revenue", "売上高", ["jppfs_cor:NetSales", "jppfs_cor:NetSalesOfCompletedConstructionContractsCNS", "jppfs_cor:OperatingRevenue1", "jppfs_cor:Revenue"], "JPY", D, r"^売上高(合計)?$|^完成工事高$"),
    ("PL", "Cost of sales", "売上原価", ["jppfs_cor:CostOfSales", "jppfs_cor:CostOfSalesOfCompletedConstructionContractsCNS"], "JPY", D, r"^売上原価(合計)?$"),
    ("PL", "Gross profit", "売上総利益", ["jppfs_cor:GrossProfit"], "JPY", D, r"^売上総利益"),
    ("PL", "SG&A expenses", "販売費及び一般管理費", ["jppfs_cor:SellingGeneralAndAdministrativeExpenses"], "JPY", D, r"^販売費及び一般管理費(合計)?$"),
    ("PL", "Operating profit", "営業利益", ["jppfs_cor:OperatingIncome"], "JPY", D, r"^営業利益"),
    ("PL", "Ordinary profit", "経常利益", ["jppfs_cor:OrdinaryIncome"], "JPY", D, r"^経常利益"),
    ("PL", "Profit attributable to owners of parent", "親会社株主に帰属する当期純利益", ["jppfs_cor:ProfitLossAttributableToOwnersOfParent"], "JPY", D, r"^親会社株主に帰属する当期純"),
    ("KPI", "EPS (basic)", "1株当たり当期純利益", ["jpcrp_cor:BasicEarningsLossPerShareSummaryOfBusinessResults"], "JPY/share", D, None),
    ("KPI", "EPS (diluted)", "潜在株式調整後1株当たり当期純利益", ["jpcrp_cor:DilutedEarningsPerShareSummaryOfBusinessResults"], "JPY/share", D, None),
    ("KPI", "BPS", "1株当たり純資産額", ["jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults"], "JPY/share", I, None),
    ("Dividend", "Dividend per share", "1株当たり配当額", ["jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults"], "JPY/share", D, None),
    ("BS", "Total assets", "総資産", ["jppfs_cor:Assets"], "JPY", I, r"^資産合計$"),
    ("BS", "Total liabilities", "負債合計", ["jppfs_cor:Liabilities"], "JPY", I, r"^負債合計$"),
    ("BS", "Net assets", "純資産", ["jppfs_cor:NetAssets"], "JPY", I, r"^純資産合計$"),
    ("KPI", "Equity ratio", "自己資本比率", ["jpcrp_cor:EquityToAssetRatioSummaryOfBusinessResults"], "ratio", I, None),
    ("KPI", "ROE", "自己資本利益率", ["jpcrp_cor:RateOfReturnOnEquitySummaryOfBusinessResults"], "ratio", D, None),
    ("KPI", "P/E as stated in report", "株価収益率", ["jpcrp_cor:PriceEarningsRatioSummaryOfBusinessResults"], "times", D, None),
    ("CF", "Operating cash flow", "営業活動によるキャッシュ・フロー", ["jppfs_cor:NetCashProvidedByUsedInOperatingActivities"], "JPY", D, r"^営業活動によるキャッシュ・フロー$"),
    ("CF", "Investing cash flow", "投資活動によるキャッシュ・フロー", ["jppfs_cor:NetCashProvidedByUsedInInvestmentActivities"], "JPY", D, r"^投資活動によるキャッシュ・フロー$"),
    ("CF", "Financing cash flow", "財務活動によるキャッシュ・フロー", ["jppfs_cor:NetCashProvidedByUsedInFinancingActivities"], "JPY", D, r"^財務活動によるキャッシュ・フロー$"),
    ("CF", "FX effect on cash", "現金及び現金同等物に係る換算差額", ["jppfs_cor:EffectOfExchangeRateChangeOnCashAndCashEquivalents"], "JPY", D, None),
    ("CF", "Net change in cash", "現金及び現金同等物の増減額", ["jppfs_cor:NetIncreaseDecreaseInCashAndCashEquivalents"], "JPY", D, r"^現金及び現金同等物の増減額"),
    ("CF", "Cash and equivalents, end of period", "現金及び現金同等物の期末残高", ["jppfs_cor:CashAndCashEquivalents"], "JPY", I, r"^現金及び現金同等物の期末残高$"),
    ("CF", "Purchase of PP&E", "有形固定資産の取得による支出", ["jppfs_cor:PurchaseOfPropertyPlantAndEquipmentInvCF"], "JPY", D, r"^有形固定資産の取得による支出$"),
    ("CF", "Purchase of intangible assets", "無形固定資産の取得による支出", ["jppfs_cor:PurchaseOfIntangibleAssetsInvCF"], "JPY", D, r"^無形固定資産の取得による支出$"),
    ("CF", "Depreciation", "減価償却費", ["jppfs_cor:DepreciationAndAmortizationOpeCF", "jppfs_cor:DepreciationOpeCF"], "JPY", D, r"^減価償却費$"),
    ("BS", "Real estate for sale", "販売用不動産", ["jppfs_cor:RealEstateForSale", "jppfs_cor:RealEstateForSaleCNS"], "JPY", I, r"^販売用不動産"),
    ("BS", "Costs on real estate business", "不動産事業支出金", ["jppfs_cor:CostsOnRealEstateBusiness"], "JPY", I, r"^不動産事業支出金$"),
    ("BS", "Real estate for sale in process", "仕掛販売用不動産", ["jppfs_cor:RealEstateForSaleInProcess"], "JPY", I, r"^仕掛販売用不動産$"),
    ("BS", "Costs on uncompleted construction contracts", "未成工事支出金", ["jppfs_cor:CostsOnUncompletedConstructionContractsCNS", "jppfs_cor:CostsOnUncompletedConstructionContracts"], "JPY", I, r"^未成工事支出金$"),
    ("BS", "Real estate for development", "開発用不動産", [], "JPY", I, r"^開発用不動産$"),
    ("BS", "Merchandise and finished goods", "商品及び製品", ["jppfs_cor:MerchandiseAndFinishedGoods"], "JPY", I, r"^商品及び製品$"),
    ("BS", "Raw materials and supplies", "原材料及び貯蔵品", ["jppfs_cor:RawMaterialsAndSupplies"], "JPY", I, r"^原材料及び貯蔵品$"),
    ("BS", "Short-term borrowings", "短期借入金", ["jppfs_cor:ShortTermLoansPayable", "jppfs_cor:ShortTermBorrowings"], "JPY", I, r"^短期借入金$"),
    ("BS", "Current portion of long-term borrowings", "1年内返済予定の長期借入金", ["jppfs_cor:CurrentPortionOfLongTermLoansPayable", "jppfs_cor:CurrentPortionOfLongTermBorrowings"], "JPY", I, r"^1年内返済予定の長期借入金$"),
    ("BS", "Long-term borrowings", "長期借入金", ["jppfs_cor:LongTermLoansPayable", "jppfs_cor:LongTermBorrowings"], "JPY", I, r"^長期借入金$"),
    ("BS", "Bonds payable", "社債", ["jppfs_cor:BondsPayable"], "JPY", I, r"^社債$"),
    ("BS", "Current portion of bonds", "1年内償還予定の社債", ["jppfs_cor:CurrentPortionOfBonds"], "JPY", I, r"^1年内償還予定の社債$"),
    ("BS", "Lease obligations (current)", "リース債務（流動）", ["jppfs_cor:LeaseObligationsCL"], "JPY", I, None),
    ("BS", "Lease obligations (non-current)", "リース債務（固定）", ["jppfs_cor:LeaseObligationsNCL"], "JPY", I, None),
    ("Shares", "Shares issued", "発行済株式総数", ["jpcrp_cor:TotalNumberOfIssuedSharesSummaryOfBusinessResults", "jpcrp_cor:NumberOfIssuedSharesAsOfFiscalYearEndIssuedSharesTotalNumberOfSharesEtc"], "shares", I, None),
    ("Shares", "Treasury shares", "自己株式数", ["jpcrp_cor:TotalNumberOfSharesHeldTreasurySharesEtc", "jpcrp_cor:NumberOfSharesHeldInOwnNameTreasurySharesEtc"], "shares", I, r"自己株式.*株式数"),
    ("Shares", "Average shares outstanding", "期中平均株式数", [], "shares", D, r"期中平均株式数"),
    ("Employees", "Employees (consolidated)", "従業員数", ["jpcrp_cor:NumberOfEmployees"], "persons", I, r"^従業員数"),
]
SUMMARY_TO_STATEMENT = {  # 5-year summary elements used for the unit/scale check
    "Revenue": "jpcrp_cor:NetSalesSummaryOfBusinessResults",
    "Ordinary profit": "jpcrp_cor:OrdinaryIncomeLossSummaryOfBusinessResults",
    "Profit attributable to owners of parent": "jpcrp_cor:ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults",
    "Total assets": "jpcrp_cor:TotalAssetsSummaryOfBusinessResults",
    "Net assets": "jpcrp_cor:NetAssetsSummaryOfBusinessResults",
    "Operating cash flow": "jpcrp_cor:NetCashProvidedByUsedInOperatingActivitiesSummaryOfBusinessResults",
    "Cash and equivalents, end of period": "jpcrp_cor:CashAndCashEquivalentsSummaryOfBusinessResults",
}

PARENT_LEVEL = {"Dividend per share", "Shares issued"}
TEXTBLOCK_ITEMS = {"EPS (basic)", "Average shares outstanding"}

def _per_share_note(by_el, rel, en):
    """Loss-year EPS and average shares from the 1株当たり情報 text block (prior column first, current last)."""
    blocks = [f["value"] for f in by_el.get("jpcrp_cor:NotesPerShareInformationConsolidatedFinancialStatementsTextBlock", [])
              if f["context"].startswith("CurrentYear")]
    if not blocks:
        return None
    t = blocks[0]
    if en == "EPS (basic)":
        ms = re.findall(r"１株当たり当期純(利益|損失（△）)\s*([\d,]+\.\d+)円", t)
        if len(ms) < 2:
            return None
        kind, v = ms[-1] if rel == "Current" else ms[0]
        return ("-" if "損失" in kind else "") + v.replace(",", "")
    m = re.search(r"期中平均株式数（株）\s*((?:\d{1,3}(?:,\d{3})+))\s*((?:\d{1,3}(?:,\d{3})+))", t)
    if not m:
        return None
    return (m.group(2) if rel == "Current" else m.group(1)).replace(",", "")

def _ctx_ok(ctx: str, kind: str, rel: str) -> bool:
    """Plain consolidated contexts only, e.g. CurrentYearDuration / Prior1YearInstant."""
    return ctx == f"{rel}Year{kind}"

def _num(v: str):
    v = (v or "").strip().replace(",", "")
    if v in ("", "－", "-", "―"):
        return None
    try:
        float(v)
        return v
    except ValueError:
        return None

def extract_doc(st: dict, store: CsvStore, term: str, doc: dict, prior_term: str | None,
                company_code: str = "1840", fy_ends: dict | None = None) -> dict:
    """Extract all line items for current and prior year from one annual report."""
    run_id = run_ctx(st)["run_id"]
    doc_id = doc["docID"]
    fy_ends = fy_ends or CONFIG["fiscal_years"]
    stats = {"docID": doc_id, "rows": 0, "unresolved": 0, "method": "XBRL"}
    src = {"source_type": "有価証券報告書" if doc["docTypeCode"] == "120" else "訂正有価証券報告書",
           "source_title": doc.get("docDescription") or "", "as_reported_in": doc_id,
           "company_code": company_code}
    try:
        facts = read_xbrl_csv(download(st, doc_id, 5))
        method = "XBRL"
    except (FetchError, zipfile.BadZipFile, UnicodeDecodeError, StopIteration) as e:
        log(st, "edinet_extract", {"docID": doc_id}, f"XBRL CSV failed ({e}); PDF fallback")
        return extract_pdf_fallback(st, store, term, doc, prior_term, company_code, fy_ends)
    if not facts:
        return extract_pdf_fallback(st, store, term, doc, prior_term, company_code, fy_ends)
    by_el: dict[str, list[dict]] = {}
    for f in facts:
        by_el.setdefault(f["element"], []).append(f)
    for rel, t in (("Current", term), ("Prior1", prior_term)):
        if not t:
            continue
        pe = fy_ends[t]
        ps = (dt.date.fromisoformat(pe) - dt.timedelta(days=364)).replace(day=1).isoformat()
        for stmt, en, ja, els, unit, kind, label_re in LINE_ITEMS:
            hit, used = None, ""
            for el in els:
                for f in by_el.get(el, []):
                    if _ctx_ok(f["context"], kind, rel) and _num(f["value"]) is not None:
                        hit, used = f, el
                        break
                if hit:
                    break
            if not hit and en in PARENT_LEVEL:
                for el in els:
                    for f in by_el.get(el, []):
                        if f["context"] == f"{rel}Year{kind}_NonConsolidatedMember" and _num(f["value"]) is not None:
                            hit, used = f, el
                            break
                    if hit:
                        break
            if not hit and en in TEXTBLOCK_ITEMS:
                tb = _per_share_note(by_el, rel, en)
                if tb is not None:
                    hit, used = {"value": tb, "context": f"{rel}Year{kind}"}, "jpcrp_cor:NotesPerShareInformationConsolidatedFinancialStatementsTextBlock"
            if not hit and label_re:
                for f in facts:
                    if f["context"] == f"{rel}Year{kind}" and re.search(label_re, f["label"]) and _num(f["value"]) is not None \
                            and f["cons"] in ("連結", "その他", ""):
                        hit, used = f, f["element"]
                        break
            row = base_row(record_id=record_id(doc_id, en, "consolidated", pe, doc_id),
                           fiscal_term=t, statement=stmt, scope="consolidated", line_item_ja=ja,
                           line_item_en=en, xbrl_element=used, unit=unit, scale="1",
                           period_label=f"FY ending {pe}", period_start=ps if kind == D else "",
                           period_end=pe, source_url=doc_url(doc_id),
                           location_in_source=f"XBRL context {rel}Year{kind}" + (f", element {used}" if used else ""),
                           extraction_method=method, **src)
            if hit:
                row["value"] = _num(hit["value"])
                if hit["context"].endswith("_NonConsolidatedMember"):
                    row["scope"] = "non-consolidated"
                    row["record_id"] = record_id(doc_id, en, "non-consolidated", pe, doc_id)
                    row["location_in_source"] = f"XBRL context {hit['context']}, element {used} (parent-company item)"
                if used.endswith("TextBlock"):
                    row["location_in_source"] = f"XBRL context {rel}Year{kind}, element {used} (１株当たり情報 note)"
                    row["correction_note"] = "Read from the 1株当たり情報 note text (summary table shows －)."
                if rel == "Prior1":
                    row["correction_note"] = "Prior-year comparative column as reported in this filing."
            else:
                dash = any(f["context"] == f"{rel}Year{kind}" and (f["value"] or "").strip() in ("－", "-", "―")
                           for el in els for f in by_el.get(el, []))
                why = ("Filing reports － (not disclosed or not applicable, e.g. ratio not stated in a loss year)" if dash
                       else "Item not present in the filing's XBRL (e.g. no such balance)")
                row = unresolved(row, f"{why} for {ja} ({rel} year); not imputed.")
                stats["unresolved"] += 1
            store.upsert(row, run_id)
            stats["rows"] += 1
        # segments
        stats["rows"] += extract_segments(st, store, facts, t, pe, ps, rel, doc_id, src)
    # five-year summary (for the unit/scale check; stored as KPI rows)
    for en, el in SUMMARY_TO_STATEMENT.items():
        for f in by_el.get(el, []):
            m = re.fullmatch(r"(Current|Prior\d)Year(Duration|Instant)", f["context"])
            if not m or _num(f["value"]) is None:
                continue
            back = 0 if m.group(1) == "Current" else int(m.group(1)[5:])
            tt = _shift_term(fy_ends, term, -back)
            if not tt:
                continue
            pe = fy_ends[tt]
            row = base_row(record_id=record_id(doc_id, en + " (5-year summary)", "consolidated", pe, doc_id),
                           fiscal_term=tt, statement="KPI", scope="consolidated",
                           line_item_ja=f["label"], line_item_en=en + " (5-year summary)", xbrl_element=el,
                           unit="JPY", scale="1", period_label=f"FY ending {pe}", period_end=pe,
                           source_url=doc_url(doc_id), location_in_source=f"主要な経営指標等の推移, context {f['context']}",
                           extraction_method="XBRL", value=_num(f["value"]), **src)
            store.upsert(row, run_id)
            stats["rows"] += 1
    stats["rows"] += extract_orders(st, store, doc, term, fy_ends[term], src)
    log(st, "edinet_extract", {"docID": doc_id, "term": term}, f"{stats['rows']} rows, {stats['unresolved']} unresolved")
    return stats

def _shift_term(fy_ends, term, delta):
    keys = list(fy_ends)
    if term not in keys:
        return None
    i = keys.index(term) + delta
    return keys[i] if 0 <= i < len(keys) else None

SEG_ITEMS = [  # (en, ja, element candidates, label regex)
    ("Segment revenue (external customers)", "外部顧客への売上高", ["jpcrp_cor:RevenuesFromExternalCustomers"], r"外部顧客への売上高"),
    ("Segment revenue (intersegment)", "セグメント間の内部売上高又は振替高", ["jpcrp_cor:TransactionsWithOtherSegments"], r"セグメント間の内部売上高"),
    ("Segment revenue (total)", "売上高（セグメント計）", ["jpcrp_cor:Revenues", "jppfs_cor:NetSales"], r"^(売上高|計)$"),
    ("Segment profit", "セグメント利益又は損失（△）", ["jpcrp_cor:SegmentProfitLoss", "jpcrp_cor:ProfitLossOfReportableSegments", "jppfs_cor:OperatingIncome"], r"セグメント利益"),
]

def extract_segments(st, store, facts, term, pe, ps, rel, doc_id, src) -> int:
    run_id = run_ctx(st)["run_id"]
    n = 0
    pat = re.compile(rf"^{rel}YearDuration_(.*?)(ReportableSegments?Member|ReconcilingItemsMember|OtherSegmentMember|"
                     rf"AllOtherSegmentsMember|TotalOfReportableSegmentsAndOthersMember)$")
    seen = set()
    for f in facts:
        m = pat.match(f["context"])
        if not m or _num(f["value"]) is None:
            continue
        for en, ja, els, lre in SEG_ITEMS:
            if f["element"] in els or (not any(x["element"] in els for x in facts) and re.search(lre, f["label"])):
                member = m.group(1).split("_")[-1] + m.group(2)
                seg = _segment_name(member)
                key = (en, seg)
                if key in seen:
                    continue
                seen.add(key)
                row = base_row(record_id=record_id(doc_id, en, f"segment:{seg}", pe, doc_id),
                               fiscal_term=term, statement="Segment", scope=f"segment:{seg}",
                               line_item_ja=ja, line_item_en=en, xbrl_element=f["element"],
                               unit="JPY", scale="1", period_label=f"FY ending {pe}", period_start=ps,
                               period_end=pe, source_url=doc_url(doc_id),
                               location_in_source=f"セグメント情報, context {f['context']}",
                               extraction_method="XBRL", value=_num(f["value"]), **src)
                if rel == "Prior1":
                    row["correction_note"] = "Prior-year comparative column as reported in this filing."
                store.upsert(row, run_id)
                n += 1
    return n

SEGMENT_JA = {"HouseLease": "賃貸事業", "Lease": "賃貸事業", "Housing": "住宅事業", "Reform": "リフォーム事業", "Renovation": "リフォーム事業",
              "RealEstate": "不動産事業", "Rental": "賃貸事業", "Leasing": "賃貸事業", "Enlargement": "リフォーム事業",
              "ReconcilingItems": "調整額", "TotalOfReportableSegmentsAndOthers": "合計",
              "AllOtherSegments": "その他", "OtherSegment": "その他"}

def _segment_name(member: str) -> str:
    core = re.sub(r"^.*?E\d{5}-\d{3}|^jpcrp_cor", "", member)
    core = re.sub(r"(ReportableSegments?Member|Member)$", "", core)
    for k, v in SEGMENT_JA.items():
        if k.lower() in core.lower():
            return v
    return core or member

# ---------------------------------------------------------------- 受注 / 引渡 (iXBRL HTML table)
def extract_orders(st, store, doc, term, pe, src) -> int:
    """受注高 / 受注残高 per segment from the 受注実績 table in the iXBRL HTML (type 1 ZIP).

    Columns are identified by header text; the unit comes from the header, e.g. 受注高（千円）.
    A cell like '16,952,940 （18,165,110）' yields the main figure; the bracketed figure is noted.
    """
    from bs4 import BeautifulSoup
    run_id = run_ctx(st)["run_id"]
    try:
        zp = download(st, doc["docID"], 1)
    except (FetchError, BudgetStop) as e:
        log(st, "edinet_orders", {"docID": doc["docID"]}, f"type1 unavailable: {e}")
        return 0
    n = 0
    num = re.compile(r"△?[\d,]+(?:\.\d+)?")
    with zipfile.ZipFile(zp) as z:
        htmls = sorted(x for x in z.namelist() if "PublicDoc" in x and x.endswith(".htm") and "honbun" in x)
        for name in htmls:
            soup = BeautifulSoup(z.read(name), "lxml")
            for tbl in soup.find_all("table"):
                grid = [[c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])] for tr in tbl.find_all("tr")]
                if not grid or not any("受注高" in c for c in grid[0]) or not any("受注残高" in c for c in grid[0]):
                    continue
                head = grid[0]
                cols = {}
                for i, h in enumerate(head):
                    if "前年" in h or "比" in h:
                        continue
                    lab = "受注残高" if "受注残高" in h else ("受注高" if "受注高" in h else None)
                    if lab:
                        u = re.search(r"[（(](千円|百万円|円|棟)[)）]", h)
                        cols[lab] = (i, u.group(1) if u else "")
                for r in grid[1:]:
                    if len(r) < len(head) or not r[0]:
                        continue
                    seg_raw = r[0]
                    seg = re.sub(r"\s*[（(].*?[)）]\s*", "", seg_raw).strip()
                    for lab, (i, unit) in cols.items():
                        m = num.search(r[i])
                        if not m:
                            continue
                        v = m.group(0).replace(",", "").replace("△", "-")
                        extra = r[i][m.end():].strip()
                        scale = {"千円": "1000", "百万円": "1000000", "円": "1"}.get(unit, "")
                        row = base_row(record_id=record_id(doc["docID"], lab, f"segment:{seg}", pe, doc["docID"]),
                                       fiscal_term=term, statement="KPI", scope=f"segment:{seg}", line_item_ja=lab,
                                       line_item_en={"受注高": "Orders received", "受注残高": "Order backlog"}[lab],
                                       xbrl_element="", unit="units" if unit == "棟" else "JPY", scale=scale or "",
                                       period_label=f"FY ending {pe}", period_end=pe, source_url=doc_url(doc["docID"]),
                                       location_in_source=f"受注実績 table ({name.split('/')[-1]}), row '{seg_raw}', column '{head[i]}'",
                                       extraction_method="HTML", value=v, **src)
                        notes = []
                        if seg != seg_raw:
                            notes.append(f"Segment label in source: '{seg_raw}'.")
                        if extra:
                            notes.append(f"Cell also shows {extra}; see the table footnote for its meaning.")
                        row["correction_note"] = " ".join(notes)
                        if not scale:
                            row = unresolved(row, "Order table unit not stated in the column header; not imputed.")
                        store.upsert(row, run_id)
                        n += 1
            if n:
                break
    return n

# ---------------------------------------------------------------- PDF fallback
PDF_ROWS = {  # label regex in 主要な経営指標等の推移 -> (en, ja, statement, unit)
    r"^売上高": ("Revenue", "売上高", "PL", "JPY"),
    r"^経常利益": ("Ordinary profit", "経常利益", "PL", "JPY"),
    r"^親会社株主に帰属する当期純": ("Profit attributable to owners of parent", "親会社株主に帰属する当期純利益", "PL", "JPY"),
    r"^純資産額": ("Net assets", "純資産", "BS", "JPY"),
    r"^総資産額": ("Total assets", "総資産", "BS", "JPY"),
    r"^1株当たり純資産額": ("BPS", "1株当たり純資産額", "KPI", "JPY/share"),
    r"^1株当たり当期純": ("EPS (basic)", "1株当たり当期純利益", "KPI", "JPY/share"),
    r"^営業活動による": ("Operating cash flow", "営業活動によるキャッシュ・フロー", "CF", "JPY"),
    r"^投資活動による": ("Investing cash flow", "投資活動によるキャッシュ・フロー", "CF", "JPY"),
    r"^財務活動による": ("Financing cash flow", "財務活動によるキャッシュ・フロー", "CF", "JPY"),
    r"^現金及び現金同等物の期末残高": ("Cash and equivalents, end of period", "現金及び現金同等物の期末残高", "CF", "JPY"),
    r"^従業員数": ("Employees (consolidated)", "従業員数", "Employees", "persons"),
}

def extract_pdf_fallback(st, store, term, doc, prior_term, company_code, fy_ends) -> dict:
    import pdfplumber
    run_id = run_ctx(st)["run_id"]
    doc_id = doc["docID"]
    stats = {"docID": doc_id, "rows": 0, "unresolved": 0, "method": "PDF"}
    src = {"source_type": "有価証券報告書" if doc["docTypeCode"] == "120" else "訂正有価証券報告書",
           "source_title": doc.get("docDescription") or "", "as_reported_in": doc_id, "company_code": company_code}
    pe = fy_ends[term]
    try:
        pdf = download(st, doc_id, 2)
    except FetchError as e:
        for en_ja in PDF_ROWS.values():
            row = base_row(record_id=record_id(doc_id, en_ja[0], "consolidated", pe, doc_id), fiscal_term=term,
                           statement=en_ja[2], scope="consolidated", line_item_ja=en_ja[1], line_item_en=en_ja[0],
                           xbrl_element="", unit=en_ja[3], scale="", period_label=f"FY ending {pe}", period_end=pe,
                           source_url=pdf_url(doc_id), location_in_source="", extraction_method="PDF", **src)
            store.upsert(unresolved(row, f"XBRL and PDF both unavailable: {e}"), run_id)
            stats["rows"] += 1
            stats["unresolved"] += 1
        return stats
    found = set()
    with pdfplumber.open(pdf) as d:
        for pno, page in enumerate(d.pages, start=1):
            txt = page.extract_text() or ""
            if "主要な経営指標等の推移" not in txt and "連結経営指標等" not in txt:
                continue
            scale = "1000" if "千円" in txt else ("1000000" if "百万円" in txt else "")
            for tbl in page.extract_tables():
                for r in tbl:
                    if not r or not r[0]:
                        continue
                    label = re.sub(r"\s+", "", r[0])
                    for pat, (en, ja, stmt, unit) in PDF_ROWS.items():
                        if en in found or not re.search(pat, label):
                            continue
                        cells = [c for c in r[1:] if c and re.search(r"\d", c)]
                        if not cells:
                            continue
                        v = cells[-1].replace(",", "").replace("△", "-").replace(" ", "")
                        v = re.sub(r"[^\d.\-]", "", v)
                        row = base_row(record_id=record_id(doc_id, en, "consolidated", pe, doc_id), fiscal_term=term,
                                       statement=stmt, scope="consolidated", line_item_ja=ja, line_item_en=en,
                                       xbrl_element="", unit=unit, scale=("1" if unit != "JPY" else scale),
                                       period_label=f"FY ending {pe}", period_end=pe,
                                       source_url=f"{pdf_url(doc_id)}#page={pno}",
                                       location_in_source=f"主要な経営指標等の推移, p.{pno}, last column",
                                       extraction_method="PDF", value=v,
                                       correction_note="PDF fallback (XBRL unavailable); last column taken as current year.", **src)
                        if unit == "JPY" and not scale:
                            row = unresolved(row, "PDF table unit not stated on page; not imputed.")
                        store.upsert(row, run_id)
                        found.add(en)
                        stats["rows"] += 1
            if found:
                break
    for pat, (en, ja, stmt, unit) in PDF_ROWS.items():
        if en not in found:
            row = base_row(record_id=record_id(doc_id, en, "consolidated", pe, doc_id), fiscal_term=term,
                           statement=stmt, scope="consolidated", line_item_ja=ja, line_item_en=en, xbrl_element="",
                           unit=unit, scale="", period_label=f"FY ending {pe}", period_end=pe,
                           source_url=pdf_url(doc_id), location_in_source="主要な経営指標等の推移 not parsed",
                           extraction_method="PDF", **src)
            store.upsert(unresolved(row, "Not found in PDF fallback table; not imputed."), run_id)
            stats["unresolved"] += 1
            stats["rows"] += 1
    log(st, "edinet_extract", {"docID": doc_id}, f"PDF fallback: {stats}")
    return stats

def facts_for(doc_id: str) -> list[dict]:
    """Re-read cached facts (for re-extraction sampling); never downloads."""
    p = ROOT / f"raw/edinet/{doc_id}/{doc_id}_type5.csv.zip"
    return read_xbrl_csv(p) if p.exists() else []
