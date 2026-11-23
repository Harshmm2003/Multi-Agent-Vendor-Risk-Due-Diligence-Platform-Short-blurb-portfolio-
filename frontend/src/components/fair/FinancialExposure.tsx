import { useState } from "react";
import { type FairDecision, type FairOutput, type FairReport, isFairReport } from "../../api/types";
import { factLabel, inputLabel, isMoneyInput, scenarioLabel } from "../../lib/fair";
import { formatProbability, formatUsd } from "../../lib/format";
import DriversChart from "./DriversChart";
import InvestigationTimeline from "./InvestigationTimeline";
import LossExceedanceChart from "./LossExceedanceChart";

interface Props {
  fair: FairOutput | null | undefined;
  /** The meta-agent's use of these figures; absent on saved reports. */
  decision?: FairDecision;
}

/** Mirrors fair/decision.py: partial, or over half assumed, is information only. */
function notDecisionGrade(r: FairReport): string | null {
  if (r.status !== "complete") return "The agent did not finish; these are fallback estimates.";
  if (r.assumption_share > 0.5)
    return `${r.assumption_count} of ${r.input_count} inputs are assumptions, so this is indicative only.`;
  return null;
}

function Tile({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="mt-1 text-2xl font-semibold tabular-nums text-slate-900">{value}</dd>
      <p className="mt-0.5 text-xs text-slate-500">{hint}</p>
    </div>
  );
}

export default function FinancialExposure({ fair, decision }: Props) {
  const [scenarioIndex, setScenarioIndex] = useState(0);
  if (!fair) return null;
  if (!isFairReport(fair)) {
    return (
      <section className="card" aria-labelledby="fair-title">
        <h2 id="fair-title" className="section-title">Financial exposure (FAIR)</h2>
        <p className="mt-2 text-sm text-slate-600">Not quantified: {fair.skipped}.</p>
      </section>
    );
  }

  const r = fair;
  const c = r.combined;
  const caveat = notDecisionGrade(r);
  const scenario = r.scenarios[Math.min(scenarioIndex, r.scenarios.length - 1)] ?? r.scenarios[0];
  const tol = r.tolerance;

  return (
    <section className="card space-y-6" aria-labelledby="fair-title">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h2 id="fair-title" className="section-title">Financial exposure (FAIR)</h2>
          <p className="mt-1 text-sm text-slate-600">
            What this vendor could cost you in a year, from {r.scenarios.length} loss scenario
            {r.scenarios.length === 1 ? "" : "s"} simulated 10,000 times.
          </p>
        </div>
        <span className="rounded-md border border-slate-300 px-2 py-0.5 text-xs font-medium text-slate-700">
          {r.generated_by === "agent" ? "Analysed by the FAIR agent" : "Benchmark estimate"}
        </span>
      </div>

      {decision?.escalated_by_fair && (
        <div role="note" className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
          <p className="font-medium">
            Raised from {decision.base_recommendation ?? "the score-based result"} by financial exposure:
          </p>
          <ul className="mt-1 list-disc pl-5">
            {(decision.fair_reasons ?? []).map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      )}

      {(decision?.conditions ?? []).length > 0 && (
        <div className="rounded-md border border-slate-300 bg-slate-50 p-3 text-sm text-slate-800">
          <p className="font-medium">Acceptable if:</p>
          <ul className="mt-1 list-disc pl-5">
            {(decision?.conditions ?? []).map((cond) => (
              <li key={cond}>{cond}</li>
            ))}
          </ul>
        </div>
      )}

      {(caveat || decision?.fair_note) && (
        <p className="rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-700">
          {decision?.fair_note ?? caveat} It is shown for information and did not change the recommendation.
        </p>
      )}

      <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Tile label="Expected annual loss" value={formatUsd(c.ale_mean)} hint="Average across simulated years" />
        <Tile label="Chance of any loss" value={formatProbability(c.prob_any_loss)} hint="Per year" />
        <Tile label="1-in-100-year loss" value={formatUsd(c.p99)} hint="Only 1% of years are worse (P99)" />
        <Tile
          label="Chance of reaching tolerance"
          value={formatProbability(c.prob_exceeds_tolerance)}
          hint={
            tol
              ? `Per year, at ${formatUsd(tol.value_usd)} (${tol.source === "client" ? "your figure" : "derived from risk appetite"})`
              : "No tolerance given"
          }
        />
      </dl>

      {r.narrative && <p className="text-sm leading-6 text-slate-700">{r.narrative}</p>}

      <LossExceedanceChart points={c.loss_exceedance_curve} tolerance={tol?.value_usd} />

      <div className="space-y-3">
        {scenario && r.scenarios.length > 1 && (
          <div role="tablist" aria-label="Scenario" className="flex flex-wrap gap-2">
            {r.scenarios.map((s, i) => (
              <button
                key={s.run_id}
                type="button"
                role="tab"
                aria-selected={s.run_id === scenario.run_id}
                className={`rounded-md border px-3 py-1 text-sm ${
                  s.run_id === scenario.run_id
                    ? "border-slate-900 bg-slate-900 text-white"
                    : "border-slate-300 bg-white text-slate-700 hover:bg-slate-50"
                }`}
                onClick={() => setScenarioIndex(i)}
              >
                {scenarioLabel(s.scenario)} · {formatUsd(s.loss.ale_mean)}/yr
              </button>
            ))}
          </div>
        )}
        {scenario && <DriversChart drivers={scenario.drivers} current={scenario.loss.ale_mean} />}
      </div>

      {r.controls.length > 0 && (
        <div>
          <h3 id="fair-controls" className="text-sm font-medium text-slate-800">What contract terms would change</h3>
          <table aria-labelledby="fair-controls" className="mt-2 w-full text-left text-sm">
            <thead>
              <tr className="text-slate-500">
                <th className="py-1 font-medium">Control</th>
                <th className="py-1 font-medium">Scenario</th>
                <th className="py-1 font-medium tabular-nums">Expected loss, all scenarios</th>
              </tr>
            </thead>
            <tbody>
              {r.controls.map((ctl) => (
                <tr key={ctl.run_id} className="border-t border-slate-100 align-top">
                  <td className="py-1.5 pr-2 text-slate-800">
                    {ctl.title}
                    {ctl.recommended && (
                      <span className="ml-2 rounded border border-slate-300 px-1.5 text-xs text-slate-700">Recommended</span>
                    )}
                    {ctl.parameter != null && (
                      <span className="ml-1 text-xs text-slate-500">(setting {ctl.parameter})</span>
                    )}
                  </td>
                  <td className="py-1.5 pr-2 text-slate-600">{scenarioLabel(ctl.scenario)}</td>
                  <td className="py-1.5 tabular-nums text-slate-800">
                    {formatUsd(c.ale_mean)} → {formatUsd(ctl.combined_after.ale_mean)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-1 text-xs text-slate-500">
            Control effects are planning assumptions, not measured results; replace them with your own data.
          </p>
        </div>
      )}

      {r.sharpen.length > 0 && (
        <p className="text-sm text-slate-700">
          <span className="font-medium">To sharpen this estimate, tell us: </span>
          {r.sharpen.map(factLabel).join("; ")}.
        </p>
      )}

      {(r.key_findings.length > 0 || r.recommendations.length > 0) && (
        <div className="grid gap-4 sm:grid-cols-2">
          {r.key_findings.length > 0 && (
            <div>
              <h3 className="text-sm font-medium text-slate-800">Key findings</h3>
              <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-slate-700">
                {r.key_findings.map((f) => <li key={f}>{f}</li>)}
              </ul>
            </div>
          )}
          {r.recommendations.length > 0 && (
            <div>
              <h3 className="text-sm font-medium text-slate-800">Recommendations</h3>
              <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-slate-700">
                {r.recommendations.map((f) => <li key={f}>{f}</li>)}
              </ul>
            </div>
          )}
        </div>
      )}

      <details>
        <summary className="cursor-pointer text-sm font-medium text-slate-700">
          Inputs and their sources ({r.assumption_count} of {r.input_count} assumed)
        </summary>
        {r.scenarios.map((s) => (
          <div key={s.run_id} className="mt-3">
            <p className="text-sm font-medium text-slate-800">
              {s.title} <span className="font-normal text-slate-500">[{s.run_id}]</span>
            </p>
            <table className="mt-1 w-full text-left text-sm">
              <tbody>
                {s.inputs.map((i) => {
                  const fmt = isMoneyInput(i.path) ? formatUsd : (v: number) => String(Number(v.toPrecision(3)));
                  return (
                    <tr key={i.path} className="border-t border-slate-100 align-top">
                      <td className="py-1.5 pr-2 text-slate-700">{inputLabel(i.path)}</td>
                      <td className="py-1.5 pr-2 tabular-nums text-slate-800">
                        {fmt(i.min)} / {fmt(i.mode)} / {fmt(i.max)}
                      </td>
                      <td className="py-1.5 text-xs text-slate-600">
                        <code className="text-slate-700">{i.source}</code> — {i.rationale}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ))}
        {r.evidence.length > 0 && (
          <div className="mt-4">
            <p className="text-sm font-medium text-slate-800">Evidence cited</p>
            <ul className="mt-1 space-y-1 text-sm text-slate-700">
              {r.evidence.map((e) => (
                <li key={e.id}>
                  <code className="text-slate-500">{e.id}</code> {e.title}{" "}
                  <span className="text-slate-500">({e.citation})</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </details>

      <InvestigationTimeline report={r} />
    </section>
  );
}
