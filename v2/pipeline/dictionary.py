"""Data dictionary: reports/report_list.html (phase P9). Self-contained, no external scripts, phone-readable."""
from __future__ import annotations

import html

from lib import CsvStore, SCHEMAS, iso, write_text

E = html.escape

COLUMNS = {
    "record_id": ("Deterministic ID: hash of source document + line item + scope + period + as-reported-in. Re-runs update the same row.", "text", ""),
    "value": ("The data value itself. Numbers are unformatted (no thousands separators). 'not meaningful' marks a metric that cannot be computed sensibly (e.g. P/E with EPS ≤ 0). Empty when unresolved.", "see unit", ""),
    "unit": ("Unit of value: JPY, JPY/share, JPY/month, shares, persons, ratio (decimal, 0.05 = 5%), times, points, listings, items, records.", "text", ""),
    "scale": ("Multiplier: value × scale = amount in unit. EDINET XBRL values are full yen (scale 1).", "number", "1, 1000, 1000000"),
    "period_label": ("Human-readable period, e.g. 'FY ending 2025-10-31', 'quote 2026-10-07', 'observed 2026-10-09'.", "text", ""),
    "period_start": ("First day of the period (duration items).", "date", "YYYY-MM-DD"),
    "period_end": ("Last day of the period, or the balance-sheet date (instant items).", "date", "YYYY-MM-DD"),
    "source_type": ("Kind of source: 有価証券報告書, 訂正有価証券報告書, 決算短信, company website, web archive, government database, manual (student), market data (irbank, third-party), derived.", "text", ""),
    "source_title": ("Title of the source document or page.", "text", ""),
    "source_url": ("Most specific stable link to the source (EDINET viewer, PDF with #page=N, Wayback snapshot with timestamp). In the Sheet the value cell itself links here.", "URL", ""),
    "location_in_source": ("Where in the source: XBRL context/element, table, page, or snapshot hash.", "text", ""),
    "as_reported_in": ("EDINET docID of the filing that reported this value. A restated year appears once per filing.", "docID", "S1XXXXXX"),
    "access_date_jst": ("When the agent retrieved the source (JST, ISO 8601).", "datetime", ""),
    "extraction_method": ("XBRL, PDF, HTML, API, manual, web search, or derived.", "text", ""),
    "agent_status": ("Who stands behind the value (syllabus 4.3).", "enum", "agent-extracted, student-verified, student-corrected, unresolved"),
    "validation_status": ("Result of the accuracy checks.", "enum", "passed, failed:<check>, not_checked"),
    "correction_note": ("Why a value is unresolved, what failed, or what the student changed and why.", "free text", ""),
    "run_id": ("Agent run that last wrote the row.", "text", "R-YYYYMMDD-HHMM"),
    "company_code": ("Securities code.", "text", "1840"),
    "fiscal_term": ("Tsuchiya fiscal term (FY ends October 31): 第46期 = FY ending 2021-10-31 … 第50期 = FY ending 2025-10-31.", "text", "第46期–第50期"),
    "statement": ("Statement or table the item belongs to.", "enum", "PL, BS, CF, Segment, KPI, Shares, Dividend, Employees"),
    "scope": ("Consolidation scope.", "enum", "consolidated, non-consolidated, segment:<name>"),
    "line_item_ja": ("Japanese name as used in the filing (verbatim, never translated).", "text", ""),
    "line_item_en": ("English name used by the agent.", "text", ""),
    "xbrl_element": ("XBRL element ID the value was read from (jppfs_cor / jpcrp_cor taxonomy).", "text", ""),
    "report_title": ("Title of the integrated report, or 'not published'.", "text", ""),
    "section": ("Section of the integrated report.", "text", ""),
    "ticker": ("Securities code of the company the metric belongs to (Tsuchiya or a peer).", "text", "1840, 2976, 205A, 1873, 1419"),
    "company_name": ("Company name.", "text", ""),
    "metric": ("Metric name (see the metric list below).", "text", ""),
    "basis": ("actual (filed figures), actual (決算短信), company forecast (会社予想), market (quotes), derived (computed for a fiscal year), derived-current (computed from today's price).", "enum", ""),
    "formula": ("Formula used; derived rows always state it.", "text", ""),
    "input_record_ids": ("record_ids of the rows used as inputs (semicolon-separated).", "text", ""),
    "quote_time_jst": ("Time of the price quote (JST).", "datetime", ""),
    "price_type": ("intraday or close. Outside TSE hours (09:00–11:30, 12:30–15:30) the last close is used.", "enum", "intraday, close"),
    "record_type": ("Kind of alternative-data row.", "enum", "listing, homepage, legal_article, legal_score, benchmark"),
    "revenue_stream": ("Revenue-stream category mapped to Tsuchiya's reported segments (connects to the Revenue Driver Tree).", "enum",
                       "住宅事業: 注文住宅, 住宅事業: 分譲, リフォーム事業, 不動産事業: 仲介, 不動産事業: 売買, 賃貸事業, Unmapped"),
    "category_reason": ("Evidence used for the revenue-stream category.", "text", ""),
    "listing_id": ("ID of the listing on its source site.", "text", ""),
    "property_type": ("Property type as shown (新築戸建, 土地, 賃貸 2LDK, …).", "text", ""),
    "取引態様": ("Transaction mode shown on the listing: 売主, 代理, 専属専任媒介, 専任媒介, 一般媒介, 貸主, 仲介 … Empty when the source does not state it (never inferred).", "text", ""),
    "listing_entity": ("Group entity offering the listing.", "text", ""),
    "price_jpy": ("Asking price (JPY). List price, not contract price.", "JPY", ""),
    "rent_jpy": ("Monthly rent (JPY).", "JPY/month", ""),
    "land_m2": ("Land area (m²).", "m²", ""),
    "floor_m2": ("Floor area (m²).", "m²", ""),
    "built_year": ("Year built (築年).", "year", ""),
    "municipality": ("City / ward. Locations are stored to district (町丁目) level at most.", "text", ""),
    "district": ("District (町名・丁目). House and lot numbers are never stored.", "text", ""),
    "first_seen": ("First date the listing or page item was observed.", "date", ""),
    "last_seen": ("Last date it was observed. last_seen − first_seen ≈ days on market (censored while active).", "date", ""),
    "status": ("active, removed (no longer on a complete scan; not proof of a sale), archived (Wayback).", "enum", "active, removed, archived"),
    "page_type": ("Kind of company page (展示場, 分譲地, リフォームメニュー, キャンペーン, 店舗, …).", "text", ""),
    "observed_item": ("Item label seen on the page (link or heading text).", "text", ""),
    "snapshot_date": ("Date of the page snapshot (fetch date or Wayback timestamp).", "date", ""),
    "publisher": ("Publisher of a legal/news item.", "text", ""),
    "published_date": ("Publication or disposition date.", "date", ""),
    "headline": ("Headline (not the article text).", "text", ""),
    "summary_en": ("One sentence in the agent's own words.", "free text", ""),
    "topic": ("Legal topic.", "enum", "両手取引, 囲い込み, 手数料規制, 行政処分, other"),
    "mentions_tsuchiya": ("Whether the item names a Tsuchiya group entity.", "enum", "yes, no"),
}

LINE_ITEMS = {
    "Revenue": ("売上高", "Consolidated net sales.", "Top of the Revenue Driver Tree; control total for segment revenue."),
    "Cost of sales": ("売上原価", "Cost of goods/construction sold.", "Gross margin driver."),
    "Gross profit": ("売上総利益", "Revenue − cost of sales.", "Margin."),
    "SG&A expenses": ("販売費及び一般管理費", "Selling, general and administrative expenses.", "Operating leverage."),
    "Operating profit": ("営業利益", "Gross profit − SG&A.", "Forecast OP = Revenue × margin (syllabus 2.3); EBITDA input."),
    "Ordinary profit": ("経常利益", "Operating profit ± non-operating items (J-GAAP concept; no IFRS equivalent).", "Bridge to net income."),
    "Profit attributable to owners of parent": ("親会社株主に帰属する当期純利益", "Net income attributable to shareholders of the parent.", "EPS numerator."),
    "EPS (basic)": ("1株当たり当期純利益", "Net income / average shares outstanding.", "P/E denominator."),
    "EPS (diluted)": ("潜在株式調整後1株当たり当期純利益", "EPS after potential dilution (often not disclosed when no dilutive securities).", "Syllabus EPS uses diluted shares."),
    "BPS": ("1株当たり純資産額", "Net assets per share.", "Book value reference."),
    "Dividend per share": ("1株当たり配当額", "Annual dividend per share.", "Dividend yield in expected return."),
    "Total assets": ("総資産", "Balance-sheet total.", "Identity check."),
    "Total liabilities": ("負債合計", "Total liabilities.", "Identity check."),
    "Net assets": ("純資産", "Total net assets (equity incl. non-controlling interests).", "Identity check; book value."),
    "Equity ratio": ("自己資本比率", "Equity / total assets (decimal).", "Financial strength."),
    "ROE": ("自己資本利益率", "Net income / average equity (decimal).", "Capital efficiency (syllabus 2.4)."),
    "P/E as stated in report": ("株価収益率", "P/E stated in the 有報 (year-end price / EPS).", "Historical multiple range."),
    "Operating cash flow": ("営業活動によるキャッシュ・フロー", "Cash from operations.", "FCF input."),
    "Investing cash flow": ("投資活動によるキャッシュ・フロー", "Cash used in investing.", "Cash roll-forward check."),
    "Financing cash flow": ("財務活動によるキャッシュ・フロー", "Cash from financing.", "Cash roll-forward check."),
    "FX effect on cash": ("現金及び現金同等物に係る換算差額", "Exchange-rate effect.", "Cash roll-forward check."),
    "Net change in cash": ("現金及び現金同等物の増減額", "Change in cash for the year.", "Cash roll-forward check."),
    "Cash and equivalents, end of period": ("現金及び現金同等物の期末残高", "Year-end cash and equivalents.", "Net debt and EV."),
    "Purchase of PP&E": ("有形固定資産の取得による支出", "Capex on tangible assets (negative = outflow).", "FCF."),
    "Purchase of intangible assets": ("無形固定資産の取得による支出", "Capex on intangibles.", "FCF."),
    "Depreciation": ("減価償却費", "Depreciation and amortisation (CF statement).", "EBITDA = OP + D&A."),
    "Real estate for sale": ("販売用不動産", "Completed real estate held for sale (inventory).", "Inventory turns; 不動産事業."),
    "Costs on real estate business": ("不動産事業支出金", "Costs incurred on real-estate projects not yet completed (inventory).", "Inventory; 不動産事業 pipeline."),
    "Real estate for sale in process": ("仕掛販売用不動産", "Real estate for sale under development.", "Inventory."),
    "Costs on uncompleted construction contracts": ("未成工事支出金", "Work in progress on construction contracts.", "Housing pipeline."),
    "Real estate for development": ("開発用不動産", "Land held for development.", "Inventory."),
    "Merchandise and finished goods": ("商品及び製品", "Merchandise.", "Inventory."),
    "Raw materials and supplies": ("原材料及び貯蔵品", "Materials.", "Inventory."),
    "Short-term borrowings": ("短期借入金", "Short-term loans.", "Interest-bearing debt."),
    "Current portion of long-term borrowings": ("1年内返済予定の長期借入金", "Long-term loans due within a year.", "Interest-bearing debt."),
    "Long-term borrowings": ("長期借入金", "Long-term loans.", "Interest-bearing debt."),
    "Bonds payable": ("社債", "Bonds.", "Interest-bearing debt."),
    "Current portion of bonds": ("1年内償還予定の社債", "Bonds due within a year.", "Interest-bearing debt."),
    "Lease obligations (current)": ("リース債務（流動）", "Lease liabilities due within a year.", "Interest-bearing debt."),
    "Lease obligations (non-current)": ("リース債務（固定）", "Lease liabilities.", "Interest-bearing debt."),
    "Shares issued": ("発行済株式総数", "Shares issued at year end.", "Shares outstanding."),
    "Treasury shares": ("自己株式数", "Treasury shares held.", "Shares outstanding = issued − treasury."),
    "Average shares outstanding": ("期中平均株式数", "Weighted average shares in the year.", "EPS check."),
    "Employees (consolidated)": ("従業員数", "Consolidated employees at year end.", "Productivity."),
    "Segment revenue (external customers)": ("外部顧客への売上高", "Segment sales to outside customers.", "Revenue Driver Tree branches (住宅/リフォーム/不動産/賃貸)."),
    "Segment revenue (intersegment)": ("セグメント間の内部売上高又は振替高", "Sales between segments (eliminated).", "Reconciliation."),
    "Segment revenue (total)": ("売上高（セグメント計）", "Segment sales incl. intersegment.", "Segment margin base."),
    "Segment profit": ("セグメント利益又は損失（△）", "Segment operating profit.", "Segment margins m_hou, m_reno, m_rent, RE_Profit (data map 1.2–1.3)."),
    "Orders received": ("受注高", "New orders in the year (housing/renovation).", "Housing_Rev = c × (backlog + orders)."),
    "Order backlog": ("受注残高", "Orders not yet delivered at year end.", "Opening backlog for next year's forecast."),
}

METRICS = {
    "Stock Price": "Latest price with quote time. Third-party daily close (irbank) unless the student entered a JPX quote.",
    "Shares Outstanding (ex-treasury)": "発行済株式総数 − 自己株式数.",
    "Market Cap": "Price × shares outstanding.",
    "Revenue": "売上高 (actual).", "Operating Profit": "営業利益 (actual).", "Net Profit": "親会社株主に帰属する当期純利益 (actual).",
    "EPS (actual)": "Reported basic EPS.", "EPS (company forecast)": "会社予想 EPS from the latest 決算短信 or 業績予想の修正.",
    "P/E (trailing)": "Price / actual EPS; 'not meaningful' if EPS ≤ 0.",
    "P/E (forward)": "Price / company-forecast EPS; 'not meaningful' if EPS ≤ 0.",
    "Operating Cash Flow": "営業CF.", "Free Cash Flow": "営業CF + 有形・無形固定資産の取得による支出 (signed).",
    "EBITDA": "営業利益 + 減価償却費.", "EBITDA (forward)": "Forecast 営業利益 + latest actual 減価償却費 (D&A assumed flat, stated).",
    "Interest-bearing debt": "Borrowings + bonds + lease obligations.", "Net debt": "Interest-bearing debt − cash.",
    "EV": "Market cap + interest-bearing debt − cash.",
    "Target P/E": "Mean of peers' forward P/E (negative/n.m. excluded; median shown in the note).",
    "Target Price (P/E)": "Target P/E × forecast EPS; 'not meaningful' when forecast EPS ≤ 0.",
    "Expected Return (P/E)": "Target price / price − 1 + dividend yield (syllabus 2.5).",
    "Average Valuated Price": "Mean of the P/E method price (forecast EPS, else trailing, else 5-year average EPS) and the EV/EBITDA price.",
    "EPS (5-year average, actual)": "Mean of reported basic EPS over the five fiscal years (normalised EPS).",
    "Target Price (P/E, trailing EPS)": "Target P/E × latest reported annual EPS; 'not meaningful' if EPS ≤ 0.",
    "Target Price (P/E, 5-year average EPS)": "Target P/E × 5-year average EPS (normalised; used when forecast/trailing EPS ≤ 0).",
    "Expected Return (P/E, trailing EPS)": "Target price (trailing EPS) / current market price − 1 + dividend yield.",
    "Expected Return (P/E, 5-year average EPS)": "Target price (5-year average EPS) / current market price − 1 + dividend yield.",
    "Target EV/EBITDA": "Mean of peers' trailing EV/EBITDA (latest 有報 actuals), applied to Tsuchiya's forward EBITDA.",
    "Target Price (EV/EBITDA)": "(Forward EBITDA × target multiple − net debt) / shares (syllabus 2.4).",
    "Expected Return (EV/EBITDA)": "Target price / price − 1 + dividend yield.",
    "Dividend Yield (forecast)": "Forecast DPS / price.",
    "Forward P/E (irbank cross-check)": "Third-party cross-check only.",
    "EPS (company forecast, irbank cross-check)": "irbank 予想EPS; third-party cross-check of the 決算短信 value.",
    "Revenue (company forecast)": "会社予想 売上高 (latest 決算短信 / 業績予想の修正).",
    "Operating Profit (company forecast)": "会社予想 営業利益.",
    "Net Profit (company forecast)": "会社予想 親会社株主に帰属する当期純利益.",
    "DPS (company forecast)": "会社予想 1株当たり配当金 (annual total).",
    "EV/EBITDA (forward)": "Peer: (price × shares + debt − cash) / (forecast 営業利益 + latest 減価償却費).",
    "EV/EBITDA (trailing)": "Peer: (price × shares ex-treasury + interest-bearing debt − cash) / (営業利益 + 減価償却費) from the peer's latest 有報 (EDINET).",
    "Revenue (決算短信)": "売上高 as reported in the full-year 決算短信 (百万円, rounded); cross-source check input.",
    "Operating profit (決算短信)": "営業利益 as reported in the full-year 決算短信; cross-source check input.",
    "Net profit (決算短信)": "親会社株主に帰属する当期純利益 as reported in the full-year 決算短信; cross-source check input.",
}

CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--mut:#66625a;--line:#e3dfd6;--acc:#2f5d50;--chip:#eef3f0}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ecebe6;--mut:#a9a59b;--line:#33312c;--acc:#8cc2ae;--chip:#22302b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif}
main{max-width:960px;margin:0 auto;padding:24px 16px 64px}h1{font-size:1.6rem;margin:.2em 0}h2{margin-top:2.2em;border-bottom:2px solid var(--acc);padding-bottom:.2em}
h3{margin-top:1.6em}p.mut,.mut{color:var(--mut)}nav a{display:inline-block;margin:0 .6em .4em 0;color:var(--acc)}
.tbl{overflow-x:auto;border:1px solid var(--line);border-radius:8px}table{border-collapse:collapse;width:100%;min-width:560px}
th,td{text-align:left;vertical-align:top;padding:.45em .6em;border-bottom:1px solid var(--line)}th{background:var(--chip);font-weight:600}
code{font-size:.9em;background:var(--chip);padding:0 .25em;border-radius:3px}.note{border-left:3px solid var(--acc);padding:.4em .8em;background:var(--chip)}
"""

def build(stores: dict[str, CsvStore]) -> str:
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>Tsuchiya Data Dictionary</title><style>{CSS}</style></head><body><main>",
             "<h1>Tsuchiya Holdings (1840) — data dictionary</h1>",
             f"<p class='mut'>What every value in the four CSVs and Sheet tabs means. Generated {E(iso())} by the research-data agent. "
             "The agent extracts and computes; the student owns forecasts, target multiples and positions.</p>",
             "<nav>" + "".join(f"<a href='#{E(n.replace(' ', '-'))}'>{E(n)}</a>" for n in SCHEMAS) + "<a href='#method'>Methodology</a></nav>"]
    for name, cols in SCHEMAS.items():
        st = stores.get(name)
        parts.append(f"<h2 id='{E(name.replace(' ', '-'))}'>{E(name)}.csv</h2>")
        parts.append(f"<p class='mut'>{len(st.rows) if st else 0} rows. Tidy format: one row per data point.</p>")
        parts.append("<h3>Columns</h3><div class='tbl'><table><tr><th>Column</th><th>Meaning</th><th>Unit / type</th><th>Allowed values</th></tr>")
        for c in cols:
            m, t, a = COLUMNS.get(c, ("(undocumented column)", "", ""))
            parts.append(f"<tr><td><code>{E(c)}</code></td><td>{E(m)}</td><td>{E(t)}</td><td>{E(a)}</td></tr>")
        parts.append("</table></div>")
        if not st:
            continue
        if name == "Securities Reports":
            present = sorted({r["line_item_en"] for r in st.rows.values()})
            parts.append("<h3>Line items</h3><div class='tbl'><table><tr><th>Japanese</th><th>English</th><th>Definition</th><th>Source</th><th>Model link / caveats</th></tr>")
            for it in present:
                base_it = it.replace(" (5-year summary)", "")
                ja, d, link = LINE_ITEMS.get(base_it, ("", "", ""))
                if it.endswith("(5-year summary)"):
                    d = "Same item from 主要な経営指標等の推移 (used for the unit/scale check). " + d
                srcs = sorted({r["extraction_method"] for r in st.rows.values() if r["line_item_en"] == it})
                parts.append(f"<tr><td>{E(ja)}</td><td>{E(it)}</td><td>{E(d)}</td><td>有価証券報告書 ({E(', '.join(srcs))})</td>"
                             f"<td>{E(link)} J-GAAP; restated years appear once per filing (see as_reported_in).</td></tr>")
            parts.append("</table></div>")
        elif name == "Baseline Metrics":
            present = sorted({r["metric"] for r in st.rows.values()})
            parts.append("<h3>Metrics</h3><div class='tbl'><table><tr><th>Metric</th><th>Definition / formula</th><th>Companies</th></tr>")
            for mtr in present:
                tick = sorted({r["ticker"] for r in st.rows.values() if r["metric"] == mtr})
                parts.append(f"<tr><td>{E(mtr)}</td><td>{E(METRICS.get(mtr, 'See formula column.'))}</td><td>{E(', '.join(tick))}</td></tr>")
            parts.append("</table></div>")
        elif name == "Alternative Data":
            types = sorted({(r["record_type"], r["page_type"] or r["topic"] or r["property_type"][:12]) for r in st.rows.values()})
            parts.append("<h3>Record types present</h3><ul>" + "".join(f"<li>{E(a)}: {E(b)}</li>" for a, b in types[:80]) + "</ul>")
        elif name == "Integrated Reports":
            present = sorted({(r["line_item_ja"], r["line_item_en"]) for r in st.rows.values()})
            parts.append("<h3>Line items</h3><ul>" + "".join(f"<li>{E(a)} — {E(b)}</li>" for a, b in present) + "</ul>")
    parts.append("""<h2 id='method'>Methodology</h2>
<h3>Sources and precedence</h3><p>有価証券報告書 XBRL &gt; 有価証券報告書 PDF &gt; 決算短信 &gt; company website &gt; third-party sites. Conflicts are recorded as validation notes, never resolved silently. Portals that forbid automated access (NIFTY不動産, 不動産ジャパン, ハトマークサイト, SUUMO, HOME'S, athome, kabutan, Yahoo!ファイナンス, minkabu) are never fetched; portal listings come only from the student's manual entries.</p>
<h3>Validation checks</h3><ul><li>総資産 = 負債 + 純資産; 売上高 − 売上原価 = 売上総利益; Σ segment external revenue = 売上高; cash roll-forward (期首 + 営業CF + 投資CF + 財務CF + 換算差額 = 期末).</li>
<li>Cross-document: the same fiscal year in consecutive 有報 must match unless a restatement/correction exists (both kept).</li>
<li>EPS plausibility (NI / average shares within 1%); unit/scale vs the 5-year summary (no ×1,000 or ×1,000,000 errors).</li>
<li>Cross-source: headline figures vs 決算短信; computed forward P/E vs irbank 予想PER within 10%.</li>
<li>Re-extraction: 10% of rows written each run are re-read from the cached file. Listings: duplicates flagged; every listing has a source URL.</li></ul>
<h3>Revenue-stream rules</h3><p>取引態様 first: 売主 → 不動産事業: 売買 (住宅事業: 分譲 when the seller is 土屋ホーム); 専任/専属専任/一般媒介, 仲介, 代理 → 不動産事業: 仲介; rental with 貸主 → 賃貸事業; rental via 仲介 → 不動産事業: 仲介. No 取引態様 → Unmapped (never inferred). Homepage items are mapped by the page's business (展示場 → 住宅事業: 注文住宅; 分譲地 → 住宅事業: 分譲; ホームトピア → リフォーム事業; 土屋ホーム不動産 → 不動産事業: 仲介).</p>
<h3>Legal penalty score (0–100)</h3><p>(a) MLIT administrative dispositions against group entities, last 5 years: 50 × min(1, Σ severity / 4) with 免許/許可取消 = 4, 業務/営業停止 = 2, 指示 = 1, 行政指導/勧告 = 0.5. (b) News/court items naming a group entity in a brokerage legal context: 30 × min(1, n / 3). (c) Share of observed group brokerage sale listings under 専任/専属専任媒介: 10 × share. (d) Brokerage regulation in force, verified against primary sources: 10 × points / 10. Missing components are excluded and the available weight is stated.</p>
<p class='note'>This is an exposure measure, not a finding of wrongdoing.</p>
<h3>Known caveats</h3><ul><li>List prices are not contract prices; a listing that disappears may have been withdrawn, not sold.</li>
<li>irbank prices are third-party daily closes; verify against JPX before relying on them.</li>
<li>Forward EBITDA assumes D&amp;A equal to the latest actual year.</li>
<li>Segment definitions may change between years; check the segment note in each filing.</li></ul>
</main></body></html>""")
    return "".join(parts)

def write(stores):
    write_text("reports/report_list.html", build(stores))
    return {"written": "reports/report_list.html"}

def coverage(stores) -> dict:
    """For eval: share of columns and line items documented."""
    cols = [c for n in SCHEMAS for c in SCHEMAS[n]]
    col_ok = sum(1 for c in cols if c in COLUMNS)
    items = {r["line_item_en"].replace(" (5-year summary)", "") for r in stores["Securities Reports"].rows.values()} if "Securities Reports" in stores else set()
    item_ok = sum(1 for i in items if i in LINE_ITEMS)
    mets = {r["metric"] for r in stores["Baseline Metrics"].rows.values()} if "Baseline Metrics" in stores else set()
    met_ok = sum(1 for m in mets if m in METRICS)
    return {"columns": f"{col_ok}/{len(cols)}", "line_items": f"{item_ok}/{len(items)}", "metrics": f"{met_ok}/{len(mets)}",
            "missing_line_items": sorted(items - set(LINE_ITEMS)), "missing_metrics": sorted(m for m in mets if m not in METRICS)}
