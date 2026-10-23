---
name: forecast-discount
description: Md, the forecast discount model: the signed yen adjustment subtracted from the company's operating-profit
  forecast before valuation, and the EPS discount derived from it. Use to explain Md or its parts.
---
1. Read with `peek`: Md (operating profit), Md (EPS), Md: Discount Baseline, Md: Land Price Ave, Md: Supply Costs.
2. Discount Baseline comes from Tsuchiya's past forecasts vs actuals. Land Price Ave and Supply Costs are logged
   as % changes since the guidance date; their coefficients are 0 until fitted on the panel, so they do not yet
   change Md. Say so if asked.
3. Md is applied as target multiple × (forecast − Md). Never change it or suggest a different value.
