---
name: forecast-inputs
description: Which earnings forecast the valuation uses and why: company guidance, the P/E floor (F2), clean
  normalised EPS (F3) and forward depreciation (F7). Use to explain a forecast-related number.
---
1. Read the rows with `peek`: EPS (company forecast), EPS (normalised, ordinary profit), Depreciation (forward),
   EPS basis used.
2. Explain the basis in the order the code applies it: forecast EPS if it passes the floor, else trailing, else
   normalised. Forward depreciation: year-to-date scaled, else capex roll-forward, else flat.
3. Quote each row's note; do not recompute.
