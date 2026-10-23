---
name: peer-multiples
description: The peer-average method (M0): target P/E and EV/EBITDA from the peer set, applied to Tsuchiya. Use to
  explain or re-run the P/E and EV/EBITDA prices.
---
1. Values are computed by code in P6 and in every `steady_refresh`. Re-run with `run_phase` P6 only if P6 failed.
2. EV/EBITDA has two rows: forward (peers' forecast operating profit, F1) and trailing (years matched). Both are
   reported; say which one feeds the combined value.
3. Read results with `peek` (metric=Target Price (EV/EBITDA, forward), Target Price (P/E)…). Never adjust them.
