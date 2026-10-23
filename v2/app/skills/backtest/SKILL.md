---
name: backtest
description: The panel backtest: rank IC with a Newey-West t, top-minus-bottom quintile, hit rate and value error per method,
  using only data published at each month-end. Use to explain which methods predicted later returns.
---
1. Read the measures per method from `read_research`.
2. A method is accepted only if its mean IC > 0 and t ≥ 2 (fixed before the first run). Say which pass and which do not.
3. Mention the number of companies and months: with few companies, results are noisy.
