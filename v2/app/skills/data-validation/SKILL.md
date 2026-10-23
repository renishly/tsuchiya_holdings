---
name: data-validation
description: Run and read the validation checks. Use after new data was written or when a check failed.
---
1. Call `run_phase` with P4. It checks balance-sheet identities, segment sums, EPS plausibility, units and
   cross-document agreement.
2. Report failed checks with their item names. Missing values stay missing: never fill them in.
