---
name: market-data
description: Refresh prices and derived metrics. Use when the task asks for current figures and all build phases are
  complete.
---
1. Call `steady_refresh` once. It reads the daily price store (updated once a day after 15:30 JST), recomputes
   the baseline metrics and, once a day, checks EDINET for new filings.
2. Report new filings and any warning it returns. Do not call it twice in one run.
