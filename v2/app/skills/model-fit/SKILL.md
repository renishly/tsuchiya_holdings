---
name: model-fit
description: M1 statistical comparables: log P/B on 3-year clean ROE, growth and log revenue; log(EV ÷ 3-year average EBITDA) on
  3-year margin, growth and log revenue; year effects; robust regression. Use to explain coefficients and Tsuchiya's fitted multiples.
---
1. Read coefficients, n, scale and Tsuchiya's fitted values from `read_research`.
2. Clean ROE = ordinary profit × (1 − 30.4%) ÷ net assets, so one-off gains and losses do not move it. Company-years whose
   latest EBITDA is below half its 3-year average are left out of the EV equation.
3. Explain the sign of each coefficient in words (e.g. higher ROE, higher P/B) and the 80% interval.
4. Never refit or adjust a coefficient.
