---
name: news-scout
description: Scan one topic's new evidence-board items and flag those that could move Tsuchiya's operating profit and are
  not covered by Md's measured variables. Use once per topic per run, when code has found new items.
---
1. Call `read_items`. Each item has an id, date, source, title, `citable` and, for some, an excerpt. Sources: company IR
   notices, EDINET filings (Tsuchiya and the M1 universe), BOJ news, MLIT press releases, フラット35 rates, and GDELT
   headlines.
2. Items with `citable: false` (GDELT headlines) are context only: use them to understand the background, never flag them.
3. Ask of each citable item: could it change this fiscal year's operating profit? Is it about something Md does not
   measure (not land prices, building costs, Tsuchiya's order intake or the guidance record)?
4. Call `flag_items` once with the qualifying ids and one-line reasons, or with an empty list.
