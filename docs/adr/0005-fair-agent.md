# ADR 0005: An agentic FAIR analyst that quantifies vendor risk in dollars

- **Status:** accepted; all 6 steps built (live re-test pending the Groq daily quota)
- **Date:** 2026-11-22

## Context

The five specialist agents score a vendor 0–100. None answers the question a
risk committee asks: *if something goes wrong with this vendor, how much could
we lose, and how likely is it?* FAIR (Factor Analysis of Information Risk) is
the standard way to answer it in dollars:

    Risk ($/yr) = Loss Event Frequency x Loss Magnitude
    LEF = Threat Event Frequency x Vulnerability
    LM  = Primary loss (productivity, response, replacement)
        + Secondary loss (fines & judgments, reputation, competitive advantage)

Every factor is a range, and a Monte Carlo simulation turns the ranges into a
distribution of annual loss.

## Decision

1. **A standalone package, `backend/fair/`, plus a thin VendorIQ adapter.**
   `fair/` never imports `app/` (enforced by a test). VendorIQ plugs in through
   ports (`fair/ports.py`: evidence provider, LLM client); a CLI is the second
   host.
2. **The LLM never produces a dollar figure.** It chooses and justifies input
   ranges; deterministic numpy code (`fair/model/`) computes every number. The
   final report's figures are filled from a run ledger, and any `$` amount in
   the narrative must match `format_usd` of a ledger value.
3. **Agentic, not a fixed pipeline.** A tool-calling loop (`fair/agent/`):
   plan scenarios → gather evidence → simulate → sensitivity analysis → test
   controls → submit. Bounded by a step/token/time budget; a deterministic
   baseline is returned (`status=partial`) if the agent cannot finish.
4. **Every input cites a source:** `client`, `assumption`, `ev_<id>`,
   `benchmark:<id>` or `derived:<id>`. Missing client data falls back:
   client input → derived from the profile → public data → cited benchmark →
   labelled assumption. More than 50% assumptions = not decision-grade.
5. **The meta-agent stays deterministic and reads FAIR as an overlay.** The
   existing 12-rule chain is unchanged; `evaluate_fair(FairDecisionSignals)`
   returns none / review / reject and the final result is the stricter of the
   two. FAIR can only add caution. A derived (not client-stated) tolerance can
   cause REVIEW, never REJECT. FAIR is not part of the weighted composite.

## Math choices (step 1)

- **Beta-PERT** for three-point estimates (λ = 4; mean = (min + 4·mode + max)/6).
- **Poisson** event counts per simulated year with rate TEF × Vulnerability,
  capped at 1 for one-shot scenarios (insolvency, sanctions exit).
- Secondary loss follows each event with probability `secondary_probability`.
- Seed = `derive_seed(evaluation_id, scenario)`; draws in a fixed order, so a
  re-run is identical. Scenarios combine by adding samples (independent seeds).
- Exceedance is "loss ≥ X" for both the curve and `prob_exceeds`, so a loss
  exactly at the tolerance counts as reaching it.
- **Altman Z** (manufacturers) and **Z''** (others) with the published zone
  cut-offs. Each zone's annual default probability is centred on the S&P
  long-run default rate of the rating band it approximates (safe ≈ BBB 0.22%,
  grey ≈ speculative grade 4.1%, distress ≈ CCC/C 28.3%), with wide ranges.

## Agent choices (step 2)

- **Cited benchmarks** live in `fair/data/benchmarks.json` (IBM Cost of a Data
  Breach 2025/2026, Verizon DBIR 2025, Cyentia IRIS 2025, S&P default studies,
  CCPA and GDPR fine levels), each with source, URL and year, checked
  2026-11-22. An input may cite only ids in that file.
- **Baseline first:** `get_baseline_inputs` gives a cited starting point per
  scenario; the agent refines it with evidence. The same baseline is the
  fallback when the agent cannot finish.
- **Guardrails enforced in code:** argument validation (errors go back to the
  model), evidence and benchmark ids must exist, a loss-event frequency
  benchmark forces vulnerability = 1 (no double counting), one final run per
  scenario, and every `$` figure in the narrative must match a tool's output.
- **Budget:** 12 model calls, 24 tool calls, 300 s. Exceeding any of them, two
  rejected submissions, or an API failure gives a `partial` report from the
  agent's last runs plus baselines.

## Token use (Groq free tier: 8,000 tokens/minute, 200,000 tokens/day)

Every turn resends the system prompt, the tool schemas and the history, so
tokens grow with turns, not with work. Measured on the healthcare example:

| Change | Model calls | Tokens | Time |
|---|---|---|---|
| First version | timed out | not measured | >180 s |
| Pacing, history compaction, `reasoning_effort=low`, compact JSON | 8 | 22,981 | 3:05 |
| Tools take lists; slimmer schemas (titles and repeated docstrings dropped) | 5 | 17,198 | 3:05 |
| Pacing calibrated on real token counts | 5 | 17,053 | 2:04 |
| Financials + Altman merged; rounding-aware figure check | 4 to simulate | 13,136 | ~2:00 |

What each does:
- **Lists, not one call per item.** `gpt-oss-120b` on Groq returns one tool
  call per reply even with `parallel_tool_calls`, so tools take lists of
  scenarios, domains and runs.
- **History compaction** (`fair/agent/compaction.py`): a tool result the
  model has seen is resent in short form (ids and figures kept; details
  dropped; a baseline kept until its scenario is simulated).
- **Pacing** (`fair/agent/pacing.py`): waits for room in a one-minute window
  instead of being throttled; estimates scale the previous call's real count.
- **Budget** includes `max_total_tokens` (60,000) and the report shows tokens
  in/out and seconds paced. A daily-limit error stops at once (no retries)
  and the fallback report is returned.
- At ~15K tokens per analysis, a free key covers about a dozen standalone
  analyses a day, fewer when VendorIQ's five agents share the key.

## Lessons from live runs

- The first run lowered both the loss-event frequency and the vulnerability
  for the same SOC 2 evidence. A guardrail now refuses that.
- For rare events P90 is $0 while P99 is millions. Reports describe the
  chance of any loss and P99 instead, and the meta-agent rules in step 3 use
  the probability of exceeding tolerance, not P90 alone.
- An exact-string figure check rejected a correct "$4.452M" twice. Figures
  now pass if they correctly round a real tool value to 2+ significant
  figures; "$2K" for $1.5K is still refused, with the nearest real figure
  suggested.
- Unchanged ranges keep the baseline's rationale; a changed range needs one.
  Several evidence ids may back one range ("ev_1, ev_3").
- Provider errors are stored without account ids.

End-to-end runs in VendorIQ (healthcare client / Veeva; CVS Health / Veeva):

- The narrative called a vendor "low risk" while P99 was 34× the tolerance.
  A tail-risk note is now written in code whenever P99 exceeds tolerance;
  a prompt rule alone was not enough (the model still wrote "comfortably
  within tolerance" once).
- "Acceptable if: share fewer records" was offered although, with that
  control, expected loss stayed above the contract value and the uncovered
  P99 stayed far above tolerance. A control now "fixes" the exposure only if
  no VendorIQ REVIEW trigger would remain.
- In the pipeline, the other five agents use the minute's tokens just before
  FAIR runs, so it waited 300s on pacing and nearly hit its time budget.
  Rate-limit waits no longer count against that budget.
- The model once wrote its full submission as JSON text. It is now read as
  the submit_report call (same guardrails), saving a model call (~5K tokens).
- Measured: 29K and 43K tokens, 6 and 8 model calls, per FAIR run in the
  pipeline (more than standalone, because it tests controls and runs
  sensitivity there).

## VendorIQ integration (step 3)

- **Adapter** (`app/integrations/fair_adapter.py`): the five agents' outputs
  become citable evidence (score, up to 4 red flags and 4 facts each, texts
  trimmed to keep tokens down; certification tiers under compliance); the
  scraper now also collects the Altman balance-sheet lines under
  `financial_data["_altman_inputs"]` (the underscore keeps them out of the
  five agents' prompts). The client's industry drives breach costs.
- **Pipeline**: FAIR runs after the five agents and before the meta-agent,
  with the same 12 s spacing; any failure leaves `fair_output = None` and the
  evaluation completes. It is **skipped** when an OFAC match or a dealbreaker
  already rejects the vendor (FAIR could not change that), saving ~15K tokens.
- **Settings**: `FAIR_ENABLED`, `FAIR_TOKENS_PER_MINUTE` (8000),
  `FAIR_REASONING_EFFORT` (low), `FAIR_MAX_SECONDS` (300).
- **API**: `client_profile.exposure` (optional) on `POST /evaluate`; results
  carry `financial_exposure`, `fair_effect`, `escalated_by_fair`,
  `base_recommendation` and the full report under `fair`.
- **Storage**: migration `0002` adds `evaluations.fair_analysis` (JSONB) and
  `fair_ale_mean`, `fair_prob_exceeds_tolerance`.

**Meta-agent rules as built** (`evaluate_fair` / `apply_fair_rules`). P90 is
often $0 for rare events, so the rules use expected loss and the chance of
reaching the tolerance instead of P90:

| Condition | Effect |
|---|---|
| No report, skipped, partial, or > 50% assumed inputs | none (shown as information) |
| Client-stated tolerance, P50 ≥ tolerance, confidence ≥ 50% | REJECT |
| Expected annual loss ≥ tolerance (client or derived) | REVIEW |
| Chance per year of reaching tolerance ≥ 10% | REVIEW |
| 1-in-100-year loss minus liability cap and insurance > tolerance | REVIEW |
| Expected annual loss > annual contract value | REVIEW |

The final recommendation is the stricter of the existing rule chain and the
FAIR effect, so FAIR never softens a decision. Escalations say so in the
decision reason ("Scores alone would have recommended APPROVE").

## Steps 4-6

- **Common random numbers.** Each input draws from its own random stream
  (seed + input name), so changing one input never changes another's draws.
  What-if differences (sensitivity, controls) come from the change, not noise.
- **Sensitivity** (`fair/model/sensitivity.py`): each input pinned at its min
  and max; inputs ranked by the swing in expected annual loss. Computed in
  code for every final scenario (no model tokens) and offered to the agent as
  `sensitivity_analysis`.
- **Controls** (`fair/model/controls.py`): five catalog controls (SOC 2 Type 2
  re-attestation, fewer records shared, client-held encryption, exit plan and
  escrow, fallback service). Their effect sizes are planning assumptions,
  labelled as such on every range they change (the original source is kept).
  Control runs are what-ifs: they can never be a final run. Every applicable
  control is tested in code for the report; the agent may test others
  (`test_control`, e.g. a different share of records) and recommend some.
- **Conditions.** If a control brings expected loss, the typical year and the
  chance of reaching the tolerance (< 10%) within tolerance, the meta-agent
  turns the finding into "REVIEW, acceptable if: <control>" instead of REJECT.
- **"To sharpen this estimate"**: the missing client facts, ordered by how
  much the inputs they feed drive the result.
- **SEC filings** (`app/services/edgar_search.py`, tool `search_sec_filings`):
  three fixed EDGAR full-text searches (8-K Item 1.05 cyber incidents,
  going-concern doubt, privacy consent orders), filtered to the vendor's CIK.
  The API takes one form type per request.
- **Outage and sanctions exit** baselines. Outage uses the client's revenue at
  risk per hour when given; its frequency is a labelled assumption (no cited
  benchmark yet). Sanctions exit follows the OFAC result. VendorIQ adds outage
  for high/critical services and sanctions exit for a possible OFAC match.
- **Frontend** (`frontend/src/components/fair/`): optional exposure inputs on
  the form; headline tiles, the loss exceedance curve (log dollar axis,
  tolerance line, keyboard crosshair, table view), the drivers chart, the
  controls table, inputs with sources, and the investigation timeline. Dollar
  figures use the same formatting as the backend (tested on the same cases).

## Build steps

| # | Step | Status |
|---|---|---|
| 1 | Math engine, Altman Z, scenario templates, CLI, tests | **done** |
| 2 | Agent loop, core tools, guardrails, fallback, cited benchmarks | **done** |
| 3 | VendorIQ adapter, pipeline step, migration, meta-agent overlay | **done** |
| 4 | Sensitivity analysis and control testing tools | **done** |
| 5 | Frontend: exposure form, loss curve, drivers, timeline | **done** |
| 6 | SEC filing search, outage and sanctions scenarios | **done** |

## Consequences

- About 3 minutes (on the free Groq tier) and 6–12 extra LLM calls per
  evaluation once step 3 lands.
- Results depend on benchmark quality; every default must cite a source and
  year, and the report shows how many inputs were assumptions.
- `numpy` becomes a direct dependency (it was already installed via chromadb).
