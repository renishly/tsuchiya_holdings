---
name: news-scout
description: Scan one topic's new evidence-board items and flag those that could move Tsuchiya's operating profit and are
  not covered by Md's measured variables. Use once per topic per run, when code has found new items.
---
1. Call `read_items`. Each item has an id, date, source, title and, for Tsuchiya's notices, an excerpt.
2. Ask of each: could it change this fiscal year's operating profit? Is it about something Md does not measure
   (not land prices, building costs or the guidance record)?
3. Call `flag_items` once with the qualifying ids and one-line reasons, or with an empty list.
