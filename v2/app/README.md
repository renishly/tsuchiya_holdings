# Tsuchiya Holdings (1840) research-data agent — v2

v2 runs on **GitHub Actions** with **OpenRouter free models**. The single v1 agent (`v1/SKILL.md` as a Claude Code routine) is split into an **orchestrator** and two subagents: the **baseline subagent** (public-information valuation) and the **factor subagent** (a Fama–French-style market view). An alternative-data subagent and a consensus between subagents come later. The data, guardrails and helper code are v1's. The specification is `v2/app/prompt2.org` (local only; addenda A19–A33 describe v2).

## 1. How a run works

```
GitHub Actions (schedule / Run workflow button / API)
└─ v2/app/agent.py
   1. start (code)        pipeline/cli.py start: lock, state, approvals, dashboard check-in → run plan JSON
   2. routine (code)      each subagent's deterministic steps: unfinished build phases in order, else steady refresh + P9
   3. events? (code)      instruction, unfinished build, a step that failed, NEEDS ATTENTION, a >3% move in
                          Average Valuated Price / Target P/E / Target EV/EBITDA since the last review, daily check
      no events           quiet run: no model request at all
      events → review     orchestrator (model) explains what changed and why; may delegate follow-ups
        ├─ baseline subagent (model + tools): run_phase P1/P2/P3/P4/P6/P9, steady_refresh, peek, log_refusal, log_injection
        └─ factor subagent (model + tools): run_factor_model, peek, log_refusal, log_injection
   4. close-out (code)    P4 validation, P5 Sheet sync (both skipped when nothing changed), debug CSV, finish
└─ workflow commits v2/data, reports, state, logs, raw, inputs → push to main (normal push, never force)
```

Everything for the agent is in `v2/app/` (code, prompts, data, state, logs, evals); the paths below are relative to it. `v2/changelog/` has one page per update; `v2/resources/` holds local materials for prompt communication.

| Part | File | Owns |
|---|---|---|
| Orchestrator | `agent.py`, `prompts/orchestrator.md` | code: start, routine work, event detection, close-out; model: the review and any follow-up delegation |
| Model layer | `llm.py` | OpenRouter client, free-only guard, fallback models, per-run request budget, tool loop |
| Baseline subagent | `subagents/baseline.py`, `prompts/baseline.md` | P1 Discover, P2 有報 extraction, P3 統合報告書, P4 validation, P6 valuation (P/E, EV/EBITDA), P9 data dictionary, steady refresh (prices, metrics, daily new-filings check) |
| Factor subagent | `subagents/factor.py`, `prompts/factor.md`, `pipeline/factors.py` | betas of 1840 to market / size / value proxies (weekly returns, 5 years, Dimson lag), cost of equity r = JGB 1y + β × long-run premia, **factor-implied price** (the price if 1840 had earned exactly its factor-predicted return since 1, 2, 3 and 5 years ago, averaged), residual-income value for reference only. Shown next to the baseline, not in Average Valuated Price |
| Price store | `pipeline/factors.py` → `raw/market/daily_prices.csv`, `raw/market/jgb_1y.csv` | one yfinance batch per day after 15:30 JST for 1840, the 3 peers (A30–A31) and 4 ETFs; MOF 1-year JGB yield. The baseline's share prices also come from this store |
| Pipeline | `pipeline/*.py` | v1's helpers as real files: every source fetch and file write, with the guardrails (allowlist, robots.txt, rate limits, v2/-only writes, locked rows, 20% change guard, PII filter, injection screen) |
| Parked | `pipeline/altdata.py`, `pipeline/legal.py` | P7 listings/homepages and P8 legal score, kept for the future alternative-data subagent; refused while parked; `Alternative Data.csv` keeps v1's rows |

The models never run code they write. Every tool is one pipeline command, so a model can only do what the pipeline allows.

## 2. Setup (one time)

1. **OpenRouter key.** Create a key at https://openrouter.ai/settings/keys and set a small credit limit on it. You already bought the 10 credits, so the free limit is 1,000 requests a day; free models don't spend them. Never paste the key into a chat.
2. **Secrets.** On GitHub: repo `renishly/tsuchiya_holdings` → Settings → Secrets and variables → Actions → **Secrets** → New repository secret:

   | Secret | Value |
   |---|---|
   | `LLM_API_KEY` | the OpenRouter key |
   | `EDINET_API_KEY` | same as v1 |
   | `GOOGLE_APPLICATION_CREDENTIALS` | the **whole JSON text** of the service-account key (`~/.config/tsuchiya/sa.json`) |
   | `GOOGLE_SHEET_ID` | same as v1 |
   | `DASHBOARD_URL`, `DASHBOARD_API_KEY` | same as v1 |
   | `REINFOLIB_API_KEY` | optional; only the parked P7 uses it |

   To copy the JSON without opening it in an editor:
   ```bash
   pbcopy < ~/.config/tsuchiya/sa.json
   ```
3. **Variables** (same page, **Variables** tab; these are not secret):

   | Variable | Value |
   |---|---|
   | `MODEL` | `nvidia/nemotron-3-ultra-550b-a55b:free` |
   | `FALLBACK_MODELS` | `thinkingmachines/inkling:free,nvidia/nemotron-3-super-120b-a12b:free` |
   | `FREE_ONLY` | `true` |
   | `MAX_STEPS`, `MAX_LLM_REQUESTS` | optional (defaults 8 and 40) |

   If a model disappears, run **Actions → List models → Run workflow** and pick another `:free` model with `TOOLS = yes`.
4. **First run in test mode:** Actions → **Holdings agent v2** → Run workflow → mode `test`. Test mode uses v1's frozen fixtures and never touches the network, Sheet or dashboard. It runs on a throwaway copy of `v2/`'s data and commits nothing, but it does call OpenRouter, so it checks the key and the models. The run summary is on the run page; check its "Models used" line.
5. **First live run:** Run workflow with mode `scheduled`. Check the run summary, the new commit on `main` ("agent v2 run …"), the Sheet, and the dashboard. On OpenRouter's Activity page the requests should cost $0.
6. **Retire v1.** In claude.ai/code → Routines, disable `tsuchiya-1840-agent` so v1 and v2 never write the Sheet or the dashboard at the same time.

The schedule (`17 7,21 * * *` UTC, about 16:17 JST after the TSE close and 06:17 JST for the daily filings check) starts once the workflow file is on `main`. GitHub often starts scheduled runs a few minutes late.

## 3. Triggers

- **Schedule:** as above.
- **Button:** Actions → Holdings agent v2 → Run workflow. Inputs: `mode` (`scheduled`, `test`, `phase=P1`…`P9`) and an optional `instruction` to the orchestrator. The instruction is still bound by every guardrail.
- **API:** create a fine-grained GitHub token (Settings → Developer settings → Fine-grained tokens; only this repo; **Contents: Read and write**). Then run:
  ```bash
  curl -X POST -H "Authorization: Bearer YOUR_GITHUB_TOKEN" -H "Accept: application/vnd.github+json" https://api.github.com/repos/renishly/tsuchiya_holdings/dispatches -d '{"event_type":"run-agent","client_payload":{"mode":"scheduled"}}'
  ```
  A `204` response means the run was queued.
- **Locally** (from the repo root, Python 3.11+):
  ```bash
  pip install -r v2/app/requirements.txt
  ```
  ```bash
  python3 v2/app/agent.py --mode test
  ```
  Without `LLM_API_KEY` this runs the deterministic fallback. Test mode always works on a temporary copy, so `v2/` is left unchanged.

## 4. What each run records

- **Review events:** every run summary says which events triggered a review, or "none, so the model was not called". A review compares against the snapshot taken at the previous review (`v2/app/state/orchestrator.json`), so small daily moves add up until they cross 3% (`REVIEW_THRESHOLD` variable, e.g. `0.05` for 5%). The first run at or after 15:30 JST each day is always reviewed (the daily check). If the model is unavailable, the routine work is still done and the review is retried next run.
- **Debug CSV:** `v2/app/logs/debug_runs.csv`, uploaded to the dashboard as the monitor "Debug log (debug_runs.csv)". One row per subagent per run: whether a review was called and why, model requests and tokens, what the code ran, whether the model ran the subagent, delegations and reruns, rounds to a final report, steps and tool calls, early stops (step limit, loop) and what it had reached before each stop, repeated work, and whether P4/P5 ran or were skipped. `consensus_rounds` is "n/a" until there are two subagents.

- **Delegations and loops:** the run summary has a line per subagent, e.g. `Delegations: baseline x2 (8 steps, 6 tool calls; early stops: 1 step limit, 0 loop; repeated work: steady)`. The same numbers are kept per run in `v2/app/state/orchestrator.json` (`delegation_history`, last 200 runs). A re-delegation is allowed; each report tells the orchestrator what work was already done, so it can continue instead of repeating it.
- **Headline numbers:** the steady refresh and P6 return a `headline` block (price, P/E, targets, Average Valuated Price, returns), so the models don't search the CSV for them.
- **Close-out skips:** P4 validation is skipped when no CSV or the approval queue changed since the last validation. P5 Sheet sync is skipped when nothing changed since that day's sync and there is nothing needing attention. The Sheet is still synced at least once a day, so your edits in the Sheet are pulled back daily. Each skip is noted in the run summary.
- **Factor model:** the dashboard monitor "Factor model (Fama-French style)" shows the factor-implied price (overall and per start date), r, the risk-free rate, betas, alpha and the residual-income reference. Factors are **proxies** from Tokyo-listed ETFs: market = TOPIX ETF 1306 − JGB 1y; size = JPX-Nikkei Mid Small 1493 − TOPIX Core 30 1311; value = Nomura High Dividend 70 1577 − TOPIX 1306. Betas are re-estimated weekly; prices update daily.
- **Log rotation:** at the first run of a new month, `logs/run_log.jsonl` (gzipped) and `logs/run_summaries.md` move to `logs/archive/<month>/`. Nothing is deleted.

## 4b. Consensus between subagents

When there is no consensus yet, a debater's price moved more than 3% since the last consensus, a NEEDS ATTENTION item appeared, or the consensus is 7 days old, the subagents debate:

| Step | Who | What |
|---|---|---|
| Evidence board | code | every number the debate may use, under a stable key (headline, peers, factor model, 7-day history) |
| Round 1 | each subagent, independently | confidence (low/medium/high), up to 5 claims citing board keys |
| Round 2 | each subagent | sees the other's validated position; challenges, concessions, revised confidence |
| Checks | code | a claim citing an unknown key or a number not on the board is rejected and not shown to the other side |
| Consensus price | code | weighted mean of the computed prices (baseline Average Valuated Price, factor-implied price); "agreed" if within 15%, else "disputed" with the range |
| Statement | orchestrator | what each side argues, the core disagreement, what would settle it |
| Referees (optional) | Mercury Decide, Span-01 Lite | `REFEREE_MODE=shadow`: scores each turn (unsupported numbers, trade advice, legal claims, engagement, concessions) and judges which view is better supported; logged only |

Settings (repository variables): `CONSENSUS_WEIGHTING` (`equal` default, or `confidence`), `CONSENSUS_AGREE_WITHIN` (0.15), `CONSENSUS_ROUNDS` (2), `REFEREE_MODE` (`off` default, or `shadow`). Outputs: Baseline Metrics rows "Consensus Valuated Price", "Consensus range low/high"; dashboard monitor "Consensus valuation"; the full debate in `logs/consensus_log.jsonl`.

## 5. Approvals, corrections, inputs

Same as v1 (see `v1/README.md` §6–7), with paths under `v2/`. Approve by writing `APPROVE AR-…` on its own line in `v2/app/state/approvals.md` and committing it to `main`. Mark corrections with `student-verified` / `student-corrected` in the Sheet or the CSV. Optional inputs go in `v2/app/inputs/`.

## 6. Adding a subagent

1. Create `v2/app/subagents/<name>.py` with a `SPEC = SubagentSpec(name, description, prompt, tools, max_steps, fallback)`. Each tool should wrap a pipeline command (see `baseline.py`).
2. Write `v2/app/prompts/<name>.md`.
3. Add it to `_registry()` in `v2/app/subagents/__init__.py`. The orchestrator's `delegate` tool lists it automatically.
4. For the alternative-data subagent: un-park P7/P8 in `app/pipeline/cli.py` (`PARKED`, `DAILY_TASKS`), and give the new subagent `run_phase` for them.
5. For cross-communication: the orchestrator can already pass one subagent's report into another's task. Describe the exchange you want in `prompts/orchestrator.md`, and raise `MAX_DELEGATIONS` in `agent.py` if needed.

## 7. Troubleshooting

| In the run log | Cause → fix |
|---|---|
| `Model unavailable: LLM_API_KEY is not set` | Secret missing or misnamed. The run still did the deterministic work. |
| `not free and would spend credits` | A model ID lacks `:free`. Fix the variable, or set `FREE_ONLY=false` deliberately. |
| `couldn't serve this request` / 404 | Model retired, or it lacks tool calling. Re-run List models. |
| `still rate limited` | Free capacity is busy. The fallback did the work; backups help. |
| `402` | OpenRouter balance is negative; even free models are blocked. |
| `Sheets auth failed` | `GOOGLE_APPLICATION_CREDENTIALS` must be the JSON text, not a path. |
| `push failed` / rejected | `main` moved twice during the run. The next run commits on top; nothing is lost from state. |
| `Run skipped: paused on the dashboard` | Press Start on the dashboard card. |

## 8. Known limitations

- Everything in `v1/README.md` §9 still applies (Yahoo/irbank prices, forward EBITDA assumption), except peer forecasts: irbank blocks GitHub Actions (HTTP 403).
- **Peers (A30–A31):** タマホーム (1419), 日本ハウスHD (1873), グランディハウス (8999). Each one's forecast is read from its own IR page, which links the 決算短信 PDFs directly; its EV/EBITDA inputs come from its latest 有報 on EDINET, re-checked monthly in the daily refresh. 日本グランデ and ロゴスHD were dropped because their IR documents are served from xj-storage.jp (robots.txt disallows automated access); 積水ハウス was dropped because its website returns HTTP 403 to GitHub's servers. Past rows of former peers stay as history. J-Quants was ruled out: its terms forbid use by a class or seminar.
- **P7/P8 are parked.** Listings, homepage snapshots and the legal penalty score are not refreshed; the dashboard's legal score monitor shows v1's last value.
- **No model web search** (Addendum A22). The 統合報告書 search uses the IR-page scan only; news for the legal score is not collected.
- **Free models** can be busy or retired at short notice, and their providers may log prompts. Prompts contain only public data and run state.
- **Evals:** v1's scenario runner drives the Claude Code skill. Porting it to `v2/app/agent.py` is still to do; test mode and fixtures already work.
- **GitHub Actions** treats scheduled runs as best effort (often minutes late). Private repos get 2,000 free minutes a month; a run takes about 2–5 minutes.
