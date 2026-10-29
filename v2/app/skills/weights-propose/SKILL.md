---
name: weights-propose
description: The proposed method weights from the backtest and the approval step. Use to explain the weights and what approving
  them would change.
---
1. Read the proposed weights, the rule and the approval id from `read_research`.
2. Rule: a method is accepted if its annual backtest mean IC > 0 and t ≥ 2; weight = ½ equal share + ½ share by accepted IC.
3. Explain that nothing changes in the daily valuation until the student writes APPROVE <id> in state/approvals.md;
   a newer research run replaces an older pending request.
4. Do not recommend approving or rejecting.
