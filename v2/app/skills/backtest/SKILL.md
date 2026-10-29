---
name: backtest
description: The panel backtest: rank IC, top-minus-bottom quintile, hit rate and value error per method, using only data
  published by each date. Acceptance uses one cross-section per year. Use to explain which methods predicted later returns.
---
1. Read the measures per method from `read_research`. `backtest.methods` is the annual test (July month-ends,
   non-overlapping one-year returns, plain t); `backtest.monthly_reference` is the monthly test (overlapping returns,
   Newey-West t), shown for reference only.
2. A method is accepted only if its annual mean IC > 0 and t ≥ 2 (fixed before the first run). Say which pass and which do not.
3. Mention the number of companies and yearly cross-sections: with few years, results are noisy.
