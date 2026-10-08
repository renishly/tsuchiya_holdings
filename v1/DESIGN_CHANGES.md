# Design changes — Tsuchiya Holdings (1840) research-data agent

Final record of every change from the original build prompt, as agreed with the student on 2026-10-09.
Each entry: what the prompt said → what the agent does now → why → addendum in `prompt.md` / `prompt.org`.
Final source links are listed in §3; `SKILL.md` §7 is the enforced version of the same list.

## 1. Summary

| # | Area | Original design | Final design | Addendum |
|---|---|---|---|---|
| 1 | Deployment | Local desktop task *or* cloud routine (to confirm) | Cloud routine on private repo `renishly/tsuchiya_holdings`; commits and normal-pushes each run (never force) | A1, A11, A12, A13 |
| 2 | Peers | 9 national candidates | 日本グランデ 2976, ロゴスHD 205A, 日本ハウスHD 1873, タマホーム 1419 (Japan-only, Hokkaido-close) | A2 |
| 3 | Valuation | P/E; second method open | P/E **and** EV/EBITDA (syllabus 2.4); no P/B | A3 |
| 4 | 統合報告書 gaps | Substitutes only with approval | No substitutes; "not published" rows | A4 |
| 5 | Legal score weights | 40/20/20/20 (provisional) | **50/30/10/10**, with confirmed component formulas | A5, A12 |
| 6 | Market data | kabutan, Yahoo!ファイナンス, minkabu; irbank cross-check only | irbank daily close each run + student JPX entries (take precedence); forward P/E = price ÷ 決算短信 EPS; irbank 予想PER as cross-check | A8 |
| 7 | Allowlist | Prompt list | Group domains verified and added; peer IR sites added; banned/blocked sites removed | A9 |
| 8 | Listings | NIFTY不動産 scraping | Company-run listing pages (automatic) + student manual portal entries + MLIT area benchmark | A10 |
| 9 | Repo contents | data, reports, state, logs | Also raw/ and inputs/ | A11 |
| 10 | Revenue streams | Categories only | Explicit 取引態様-first rules; no 取引態様 → Unmapped | A12 |
| 11 | Forward EBITDA | Not specified | Forecast 営業利益 + latest actual 減価償却費 (stated) | A12 |
| 12 | Peer forecasts | Peer 決算短信 from peer IR sites | タマホーム from its own 決算短信; 日本グランデ, ロゴスHD, 日本ハウスHD from irbank 予想EPS (third-party, labeled) because their 決算短信 lists can't be read compliantly; peer EV/EBITDA on latest 有報 actuals (trailing) | A9, A14 |
| 13 | Golden values | ≥ 40 values hand-verified by the student | All 60 checked automatically against the printed PDF text; no routine human input | A15 |
| 14 | Peer fiscal years | — | 日本ハウスHD ends **April 30** (EDINET code list), not October | A2 |

## 2. Changes in detail

### 2.1 Market data (A8)
- **Removed** — kabutan.jp: 利用規約 第4条 forbids copying, storing and passing content to third parties. finance.yahoo.co.jp: help page forbids automated retrieval (scraping). minkabu.jp: 規約 7条(18) forbids mechanical retrieval.
- **Not automated** — quote.jpx.co.jp: the quote data endpoint rejects requests that don't come from the JPX web UI (HTTP 401). Working around that would bypass an access control.
- **Final** — price: [irbank daily close](https://irbank.net/1840/chart) (`price_type = close`, labeled third-party, Agent Warning until verified). Student entries in `v1/inputs/manual_prices.csv` (JPX price, quote time, screenshot) override it. Forecast EPS/OP/DPS: newest 決算短信 or 業績予想の修正 on each company's IR page. Cross-check: [irbank 予想PER/EPS](https://irbank.net/1840).
- **Consequence today** — Tsuchiya revised its FY Oct 2026 forecast on 2026-09-14 to EPS ¥0.00 ([notice](https://www.tsuchiya.co.jp/wp-content/uploads/2026/09/f30bacafc0c615eb072fc445cea37d06.pdf)). Target Price (P/E) is therefore "not meaningful", and EV/EBITDA carries the valuation. irbank still shows the pre-revision EPS of 10.09, which the cross-check flags.

### 2.2 Listings (A10)
- **Removed** — every listing portal checked forbids scraping: NIFTY不動産 (myhome.nifty.com; terms forbid scraping/crawling, and robots.txt blocks `/ikkodate/detail/*` etc.), 不動産ジャパン (fudousan.or.jp; scraping needs prior consent), and ハトマークサイト (hatomarksite.com; 規約 4条(11)). SUUMO, HOME'S and athome are excluded as well (not individually re-checked).
- **Final** — automatic daily scans of company-run pages only, with 取引態様 and 築年 read from detail pages. The student enters resale/brokered portal listings by hand (with screenshots) in `v1/inputs/manual_listings.csv`. MLIT 不動産情報ライブラリ area medians serve as a benchmark (needs `REINFOLIB_API_KEY`).
- Disappearance from a complete scan → `status = removed`, with the note "removal is not proof of a sale".

### 2.3 Disclosure sources (A9)
- **Removed** — www.release.tdnet.info (robots.txt `Disallow: /`). 決算短信 PDFs are taken from company IR pages, and JPX archive links are followed where an IR page uses them.
- **Added** — disclosure2dl.edinet-fsa.go.jp (EDINET download host and [code list](https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip)).

### 2.4 Legal score (A5, A12)
- (a) 50 × min(1, Σ severity / 4), last 5 years. Severity: 免許/許可取消 = 4, 業務/営業停止 = 2, 指示 = 1, 行政指導/勧告 = 0.5. Source: [MLIT ネガティブ情報等検索サイト](https://www.mlit.go.jp/nega-inf/). Only exact group entity names count. On 2026-10-09 the search returned 0 records for "土屋" (宅建業者, 2021–2026).
- (b) 30 × min(1, n / 3), for news/court items naming a group entity in a brokerage legal context.
- (c) 10 × the share of observed group brokerage sale listings under 専任/専属専任媒介. This depends on manual listings.
- (d) 10 × verified rule points / 10. Each rule's primary source:
  - [宅建業法 第34条の2](https://laws.e-gov.go.jp/api/2/law_data/327AC1000000176?elm=MainProvision-Article_34_2&response_format=json): 2 points.
  - [第46条](https://laws.e-gov.go.jp/api/2/law_data/327AC1000000176?elm=MainProvision-Article_46&response_format=json): 2 points.
  - [施行規則 — REINS「取引の申込みの受付に関する状況」, 2025 rule](https://laws.e-gov.go.jp/api/2/law_data/332M50004000012?response_format=json): 3 points.
  - Two rules are **source pending** and score 0 until an allowlisted primary URL is confirmed: the 2024 低廉な空家等 fee rule (1 point) and the 監督処分基準 (2 points).
- Missing components are excluded, not imputed, and the available weight is stated. Every row says "This is an exposure measure, not a finding of wrongdoing."

### 2.5 Revenue-stream rules (A12)
- The listing's 取引態様 decides the stream:
  - 売主 → 不動産事業: 売買. The exception is when the seller is 土屋ホーム, which maps to 住宅事業: 分譲.
  - 媒介, 仲介 or 代理 → 不動産事業: 仲介.
  - A rental listing where the group entity is 貸主 → 賃貸事業. A rental listing via 仲介 → 不動産事業: 仲介.
  - No 取引態様 → Unmapped. It is never inferred.
- Homepage items are mapped by the page's business:
  - 展示場 and events → 住宅事業: 注文住宅.
  - 分譲地 → 住宅事業: 分譲.
  - ホームトピア → リフォーム事業.
  - 土屋ホーム不動産 → 不動産事業: 仲介.
  - IEST. → 不動産事業: 売買.

### 2.6 Valuation (A3, A12)
- Target P/E is the mean of peers' forward P/E, with the median in the note. Negative or not-meaningful peers are excluded and listed.
- Target EV/EBITDA is the mean of peers' EV/EBITDA on their latest 有報 actuals (trailing; irbank has no forecast operating profit), applied to Tsuchiya's forward EBITDA (A14).
- Forward EBITDA = forecast 営業利益 + latest actual 減価償却費. The flat-D&A assumption is stated on every row.
- Target Price (EV/EBITDA) = (Forward EBITDA × target − net debt) / shares ex-treasury.

## 3. Final source links (what the agent fetches)

| Purpose | Final link(s) |
|---|---|
| 有価証券報告書 (API) | `https://api.edinet-fsa.go.jp/api/v2/documents.json`, `…/documents/{docID}?type=1/2/5` ([spec v2](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140206.pdf)) |
| 有報 source_url in CSVs | `https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{docID}`; PDF fallback `https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/{docID}.pdf#page=N` |
| Tsuchiya 決算短信 / forecasts | [tsuchiya.co.jp/ir/news](https://www.tsuchiya.co.jp/ir/news/) |
| Peer forecast EPS | [タマホーム 決算短信等](https://www.tamahome.jp/company/ir/library/library_02.html) (primary). [irbank 2976](https://irbank.net/2976) · [irbank 205A](https://irbank.net/205A) · [irbank 1873](https://irbank.net/1873) 予想EPS (third-party; their 決算短信 lists are JavaScript-rendered or on www.xj-storage.jp, robots.txt disallow). Optional override: `v1/inputs/manual_sources.csv` |
| Peer EV/EBITDA inputs | Each peer's latest 有報 on EDINET (営業利益, 減価償却費, borrowings, cash, shares) |
| Price (daily close) | `https://irbank.net/{ticker}/chart` (1840, 2976, 205A, 1873, 1419) |
| P/E cross-check | `https://irbank.net/{ticker}` |
| Company listings | [IEST. 物件一覧](https://iestdot.com/property/) · [土屋ホーム不動産 賃貸](https://mylist-v2.realnetpro.com/22359m1/) · [土屋ホーム不動産 物件検索 (count)](https://tsuchiyafudosan.jp/property/) |
| Group homepages | [展示場](https://www.tsuchiyahome.jp/modelhouse/) · [分譲地](https://www.tsuchiyahome.jp/realestate/) · [イベント](https://www.tsuchiyahome.jp/event/) · [ラインナップ](https://www.tsuchiyahome.jp/lineup/) · [ホームトピア](https://www.hometopia.jp/) · [不動産 サービス](https://tsuchiyafudosan.jp/service) · [店舗](https://tsuchiyafudosan.jp/store) · [IEST.](https://iestdot.com/) |
| Archived homepages | `https://web.archive.org/web/{timestamp}/{page}`; one snapshot per page per year 2021–2025 |
| Area benchmark | `https://www.reinfolib.mlit.go.jp/ex-api/external/XIT001` (API key) |
| Dispositions | [MLIT ネガティブ情報等検索サイト](https://www.mlit.go.jp/nega-inf/) (宅建業者, 建設業者) |
| Laws | e-Gov API v2 links in §2.4 |
| Manual inputs | `v1/inputs/manual_prices.csv`, `v1/inputs/manual_listings.csv` (student) |
| Never fetched | myhome.nifty.com, suumo.jp, www.homes.co.jp, www.athome.co.jp, www.fudousan.or.jp, www.hatomarksite.com, kabutan.jp, finance.yahoo.co.jp, minkabu.jp, www.release.tdnet.info, quote.jpx.co.jp, www.xj-storage.jp |

## 4. What stays the student's (syllabus)

Revenue Driver Tree, choice of KPI, forecasts, target multiple, target price, expected return, active position, field dataset and statistics. The agent supplies inputs, warnings and source links only.
