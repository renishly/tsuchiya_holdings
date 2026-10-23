---
name: data-inspection
description: Look at a few stored rows to answer a question. Use instead of guessing a value.
---
1. Call `peek` with the CSV name and filters, e.g. csv='Baseline Metrics', where=['metric=Md (operating profit)'].
2. Read at most 10 rows. Quote values with their date and source; if a value says "not meaningful" or
   "not available", report that and its note.
