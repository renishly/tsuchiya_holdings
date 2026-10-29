# Pending approval requests

Approve by writing `APPROVE <id>` (or `REJECT <id>`) on its own line in state/approvals.md.

## AR-20261009-01 — First write to the Google Sheet (create/refresh tabs)
- kind: sheets_first_write
- target: Sheet in env GOOGLE_SHEET_ID
- params: {"sheet": "GOOGLE_SHEET_ID (env)", "layout": {"Securities Reports": 25, "Integrated Reports": 22, "Baseline Metrics": 26, "Alternative Data": 44, "Agent Warnings": 8}}
- reason: One-time approval of the Sheet and tab layout: Securities Reports, Integrated Reports, Baseline Metrics, Alternative Data, Agent Warnings
- requested: 2026-10-09T08:13:47+09:00 (run R-20261009-080141)

## AR-20261023-01 — Use the new M1 coefficients and method weights in the daily valuation
- kind: model_params
- target: state/model_params.json
- params: {"version": "2026-10-23-n156"}
- reason: Research run: see reports/backtest_latest.html for the backtest, the M1 fit and the proposed weights.
- requested: 2026-10-23T17:43:41+09:00 (run R-20261023-173732)

## AR-20261025-01 — Use the new M1 coefficients and method weights in the daily valuation
- kind: model_params
- target: state/model_params.json
- params: {"version": "2026-10-25-n156"}
- reason: Research run: see reports/backtest_latest.html for the backtest, the M1 fit and the proposed weights.
- requested: 2026-10-25T06:09:33+09:00 (run R-20261025-060909)

## AR-20261030-01 — Use the new M1 coefficients and method weights in the daily valuation
- kind: model_params
- target: state/model_params.json
- params: {"version": "2026-10-30-n193"}
- reason: Research run: see reports/backtest_latest.html for the backtest, the M1 fit and the proposed weights.
- requested: 2026-10-30T06:51:41+09:00 (run R-20261030-064418)

