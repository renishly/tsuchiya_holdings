---
name: filings-ingest
description: Find and read Tsuchiya's filings (有価証券報告書 via EDINET, 統合報告書). Use when a filing phase is
  incomplete or the task asks to re-read a filing.
---
1. Phases: P1 discover filings, P2 extract the 有価証券報告書, P3 統合報告書 rows. Run them in order with `run_phase`.
2. A phase returns complete / in_progress / blocked with notes. Report "blocked" with its reason; do not retry it.
3. Never add or correct values by hand; the extraction is code.
