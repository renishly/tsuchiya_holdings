# Tsuchiya Holdings (1840) research-data agent

A Claude Code skill (`SKILL.md`) that builds and maintains the public-information baseline and first alternative-data layer for 土屋ホールディングス (1840) for the Fukuhara Seminar, Session 1 onward. It runs unattended every 5 hours as a Claude Code cloud routine, reads public sources only, never imputes a value, links every value to its source, and mirrors four CSVs into one Google Sheet.

- **Agent:** `SKILL.md`. It contains the instructions plus embedded Python helpers, which it writes to `tmp/` at run time.
- **Design changes from the original brief:** `DESIGN_CHANGES.md`.
- **Data dictionary:** `reports/report_list.html`.

## 1. What it produces

| File / Sheet tab | Content |
|---|---|
| `data/Securities Reports.csv` | 有価証券報告書 第46期–第50期 (FY Oct 2021–Oct 2025): PL, BS, CF, segments, per-share data, shares, employees, 受注高/受注残高. XBRL first, PDF fallback |
| `data/Integrated Reports.csv` | 統合報告書 per FY, or "not published" rows with the searched URLs |
| `data/Baseline Metrics.csv` | 5-year history (revenue, OP, NI, EPS, OCF, FCF, EBITDA, net debt, shares), current price, market cap, EV, P/E, peer P/E and EV/EBITDA, targets, expected return |
| `data/Alternative Data.csv` | Company-run listings, your manual portal listings, group homepage snapshots (current and Wayback), MLIT area benchmarks, legal items, legal penalty score |
| `reports/report_list.html` | What every column, line item and metric means |
| Sheet tab `Agent Warnings` (fallback file: `state/agent_warnings.csv`) | Failed checks, unresolved values, pending approvals, NEEDS ATTENTION items |

## 2. Models

| Phases | Model |
|---|---|
| Initial build P1–P4 (extraction accuracy matters most) | Claude Opus 5.5 (`claude-opus-5-5`) |
| Steady-state refresh runs | Claude Sonnet 5.5 (`claude-sonnet-5-5`) is acceptable |

Sampling uses the provider defaults.

## 3. Secrets

Names only. The values never go into the repo, CSVs, logs or the Sheet.

| Variable | What it is | Where it lives |
|---|---|---|
| `EDINET_API_KEY` | EDINET API v2 key (free) | `~/.zshrc` locally; routine environment in the cloud |
| `GOOGLE_APPLICATION_CREDENTIALS` | Service-account key. Locally a file path (`~/.config/tsuchiya/sa.json`); in the cloud the JSON text itself | outside the project |
| `GOOGLE_SHEET_ID` | ID of the Sheet (the part of its URL between `/d/` and `/edit`) | `~/.zshrc` / routine environment |
| `REINFOLIB_API_KEY` | MLIT 不動産情報ライブラリ API key (optional; area benchmarks) | same |

**Getting the EDINET key:**
1. Open https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1 and allow pop-ups.
2. Create an account with your e-mail and phone (multi-factor sign-in).
3. Fill in the contact form. The key is shown on screen afterwards.
4. Store it with:

```bash
echo "export EDINET_API_KEY='your-key'" >> ~/.zshrc
```

## 4. Google Sheets integration (done 2026-10-09)

1. Create an empty Google Sheet.
2. Create a Google Cloud project (`tsuchiya-1840-agent`) at https://console.cloud.google.com/.
3. Enable the Google Sheets API: https://console.cloud.google.com/apis/library/sheets.googleapis.com
4. Create the service account `tsuchiya-agent` (no project roles needed). Under **Keys → Add key → JSON**, download the key, then move it outside the project:
   ```bash
   mkdir -p ~/.config/tsuchiya && mv "<downloaded file>" ~/.config/tsuchiya/sa.json && chmod 600 ~/.config/tsuchiya/sa.json
   ```
5. Share the Sheet with the service-account e-mail (`tsuchiya-agent@….iam.gserviceaccount.com`) as **Editor**. Keep general access restricted.
6. Set the variables:
   ```bash
   echo "export GOOGLE_APPLICATION_CREDENTIALS=\"$HOME/.config/tsuchiya/sa.json\"" >> ~/.zshrc
   ```
   Then add `export GOOGLE_SHEET_ID='…'` to `~/.zshrc` in the same way.
7. One-cell test write. This was done with your approval: a temporary `Agent Test` tab was written, read back, then removed.

The agent's first real write needs your one-time approval. It queues `sheets_first_write` in `state/pending_approvals.md`; you approve it in `state/approvals.md` (§7). After that, each run rewrites the four tabs plus `Agent Warnings`, with one batch per tab. Value cells are `=HYPERLINK(source_url, value)`, so clicking a number opens its source.

**No-API fallback:** in the Sheet, use **File → Import → Upload** for each CSV in `data/`. Choose "Replace current sheet" in the tab with the same name, and comma as the separator.

## 5. Routine setup (cloud, private repo `renishly/tsuchiya_holdings`)

1. The repo holds the project root with `v1/` inside. `.gitignore` excludes secrets, `project secrets/`, `.env` and `v1/tmp/`.
2. Create the routine at claude.ai/code → Routines (or `/schedule` in Claude Code):
   - **Name:** `tsuchiya-1840-agent`
   - **Repository:** `renishly/tsuchiya_holdings`, default branch
   - **Schedule (cron):** `CRON_TZ=Asia/Tokyo 0 */5 * * *` (00:00, 05:00, 10:00, 15:00, 20:00 JST)
   - **Prompt:** `Read and follow v1/SKILL.md in the project root. Mode: scheduled.`
   - **Model:** Opus 5.5 until P1–P4 are complete, then Sonnet 5.5 is acceptable
   - **Environment variables:** `EDINET_API_KEY`, `GOOGLE_SHEET_ID`, `GOOGLE_APPLICATION_CREDENTIALS` (paste the JSON key text), optional `REINFOLIB_API_KEY`
   - **Network:** allow outbound access to the domains in `SKILL.md` §7
   - **Branch pushes:** allow pushes to the default branch. Each run commits `v1/data v1/reports v1/state v1/logs v1/raw v1/inputs` and pushes normally (never force), so the next run sees the state.
3. After the first scheduled run, read the newest entry in `v1/logs/run_summaries.md`.

**Manual / debug runs** (from the project root in Claude Code):
- `Read and follow v1/SKILL.md in the project root. Mode: phase=P6`: one phase, once.
- `… Mode: test`: fixtures only; no network, no Sheet.

## 5b. Seminar dashboard

The agent reports to https://fukuhara-seminar.onrender.com as **Equity Pricer [Tsuchiya Holdings]**.
- **Settings:** env `DASHBOARD_URL=https://fukuhara-seminar.onrender.com` and `DASHBOARD_API_KEY` (from the dashboard's View key). Locally, add both to `~/.zshrc`; in the cloud, add both to the `tsuchiya-1840` environment's variables and allow `fukuhara-seminar.onrender.com`.
- **Each run:** checks in (shows online for 5 h), sends a `run summary` output, red lines for NEEDS ATTENTION, and refreshes the monitors: Status, Build phases, Rows per CSV, Pending approvals, Data, Google Sheet, Data dictionary, **Current market price** (one number), **Average valuated price** (one number: mean of the P/E and EV/EBITDA prices), **Valuation (agent baseline)** (every method's price written out), Legal penalty score.
- **Pause / Start** on the card: a paused agent skips its next runs until you press Start.
- **Messages and the Inbox are off** by design.
- **Offline buffer:** if the site is asleep or unreachable, items wait in `v1/state/dashboard_buffer/` and go at the next run.

## 6. Your inputs (optional, in `v1/inputs/`)

| File | When to use | Columns |
|---|---|---|
| `manual_prices.csv` | JPX price before a session. It overrides irbank's daily close for that date | ticker, price_jpy, quote_time_jst (e.g. `2026-10-09T14:05:00+09:00`), price_type (intraday/close), source_url, screenshot_ref |
| `manual_listings.csv` | Resale/brokered listings you record by hand from NIFTY不動産 etc. (the agent never scrapes portals) | observed_date, portal, branch, listing_url, screenshot_ref, listing_id, property_type, 取引態様, listing_entity, price_jpy, rent_jpy, land_m2, floor_m2, built_year, municipality, district (no house numbers), notes |
| `manual_sources.csv` | URL of a peer's latest 決算短信 PDF when its IR list can't be read automatically | ticker, doc_date, doc_title, doc_url |

For manual listings, record the whole branch page each time, all on the same date. A listing missing from a later snapshot of the same branch is then marked `removed`.

## 7. Approvals and corrections

**Approvals.** The agent never waits for you. Anything that needs a decision is appended to `state/pending_approvals.md` as `AR-YYYYMMDD-NN`. To decide, write `APPROVE AR-…` or `REJECT AR-…` on its own line in `state/approvals.md`. Each approval covers one action.

These actions need approval:
- the first Sheet write, and any later tab or column change
- new domains
- peer set or target-method changes
- legal-score weight changes
- schema changes
- any run that would change more than 20% of a CSV's rows

**Correcting or verifying a value:**
1. **In the Sheet or the CSV**, set `agent_status` to `student-verified`, or to `student-corrected` with the new `value` and a `correction_note` saying what you changed and why.
2. **Sheet edits** are pulled back into the CSV at the start of the next sync, and the row becomes locked.
3. **Locked rows are never overwritten.** If a later extraction disagrees, the agent adds a new `unresolved` row that points to your row.
4. **In the cloud setup, the CSVs are in the repo.** To edit a CSV directly, commit to the default branch.

## 8. Evaluations

```bash
python3 v1/evals/check_outputs.py
```

```bash
python3 v1/evals/check_outputs.py --only C07 G03
```

- The first command runs every scenario twice in sandbox copies, using the frozen sources in `v1/evals/fixtures/default/` (no network, no Sheet), and writes `v1/evals/eval_report.md`.
- The second runs selected scenarios only.
- Scenarios are described in `v1/evals/scenarios.md`.
- Adversarial and out-of-scope scenarios need the `claude` CLI: add `--engine claude`.
- **Golden values:** `v1/evals/golden_values.csv` lists 60 values (12 per fiscal year) with PDF link and page. Each was checked automatically against the printed text of the official PDF page (`verified_by_pdf_check`; Addendum A15), so no hand-check is needed. You may still hand-check any row by writing `yes` in `verified_by_student`. Scenario C12 requires ≥ 40 checked values (≥ 8 per year) and a 100% match.

## 9. Known limitations

- **Prices** are Yahoo Finance daily closes via the `yfinance` library (third-party, prior close; Addendum A16 — you chose to relax the terms-of-use guardrail for this one use). If Yahoo blocks the cloud the price stays unresolved; irbank works only from your Mac. A JPX quote you enter in `inputs/manual_prices.csv` always wins.
- **Listings:** portals forbid scraping, so automatic coverage is limited to company-run pages (IEST. new builds and 土屋ホーム不動産 rentals). Resale and brokerage listings depend on your manual entries. A disappearing listing is not proof of a sale, and list prices are not contract prices.
- **Peer forecasts:** タマホーム's 決算短信 is read automatically. For 日本グランデ, ロゴスHD and 日本ハウスHD the agent uses irbank's 予想EPS (third-party, can lag a release by days), because their own lists are JavaScript-rendered or robots-blocked. Peer EV/EBITDA uses their latest annual actuals. Adding a 決算短信 URL to `inputs/manual_sources.csv` overrides irbank (optional).
- **Legal score** is an exposure measure, not a finding of wrongdoing. MLIT's database may not include every prefectural disposition. Two regulation sources score 0 until a primary URL is confirmed.
- **Corrections search:** 訂正有価証券報告書 filed far from a filing deadline are found only by the daily new-filing check (from the first run onward), not retroactively.
- **Forward EBITDA** assumes D&A equal to the latest actual year.
- **Model-behaviour evals** (prompt injection, out-of-scope requests) need the `claude` CLI to run automatically.
