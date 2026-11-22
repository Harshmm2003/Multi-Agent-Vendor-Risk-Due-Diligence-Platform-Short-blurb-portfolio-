# ADR 0005: An agentic FAIR analyst that quantifies vendor risk in dollars

- **Status:** accepted; step 1 of 6 built
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
  cut-offs. The zone → default-probability mapping is an uncalibrated
  placeholder, labelled `assumption`, until step 2 adds cited benchmarks.

## Build steps

| # | Step | Status |
|---|---|---|
| 1 | Math engine, Altman Z, scenario templates, CLI, tests | **done** |
| 2 | Agent loop, core tools, guardrails, fallback, cited benchmarks | |
| 3 | VendorIQ adapter, pipeline step, migration, meta-agent overlay | |
| 4 | Sensitivity analysis and control testing tools | |
| 5 | Frontend: exposure form, loss curve, drivers, timeline | |
| 6 | SEC filing search, outage and sanctions scenarios | |

## Consequences

- One or two minutes and 6–12 extra LLM calls per evaluation once step 3 lands.
- Results depend on benchmark quality; every default must cite a source and
  year, and the report shows how many inputs were assumptions.
- `numpy` becomes a direct dependency (it was already installed via chromadb).
