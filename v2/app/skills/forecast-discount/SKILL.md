---
name: forecast-discount
description: Md, the forecast discount model: the signed yen adjustment subtracted from the company's operating-profit
  forecast before valuation, and the EPS discount derived from it. Use to explain Md or its parts.
---
1. Read with `peek`: Md (operating profit), Md (EPS), Md: Discount Baseline, Md: Order Momentum, Md: Supply Costs,
   Md: Land Price Ave, the matching "Md term" rows, and Md: Debate Signal.
2. Discount Baseline comes from Tsuchiya's past forecasts vs actuals. The measured variables are changes since the
   guidance date: year-to-date housing orders vs last year (from a quarterly 決算短信 published after the guidance),
   the BOJ lumber index and Hokkaido land prices. Each becomes yen as coefficient × change (%) × months left / 12,
   capped at the largest past forecast miss.
3. Coefficients are fitted weekly on the M1 universe. One that passes the test fixed in advance (expected sign,
   |t| ≥ 2, at least 15 observations) is applied under the student's standing approval; one that fails is 0. Quote
   each row's note for the coefficient and the reason. Google Trends share of search is logged with coefficient 0.
4. Md is applied as target multiple × (forecast − Md). Never change it or suggest a different value.
