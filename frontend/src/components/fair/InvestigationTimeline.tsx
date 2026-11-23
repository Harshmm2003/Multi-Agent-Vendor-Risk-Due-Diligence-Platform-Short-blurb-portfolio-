import type { FairReport, FairTraceStep } from "../../api/types";

const KIND: Record<FairTraceStep["kind"], { label: string; style: string }> = {
  tool_call: { label: "Tool", style: "border-slate-300 text-slate-700" },
  thought: { label: "Note", style: "border-slate-200 text-slate-500" },
  tool_error: { label: "Corrected", style: "border-amber-300 text-amber-800" },
  guardrail: { label: "Check", style: "border-slate-400 text-slate-800" },
  fallback: { label: "Fallback", style: "border-amber-300 text-amber-800" },
  error: { label: "Error", style: "border-red-300 text-red-800" },
};

/** The agent's investigation, step by step: what it looked at and what was checked. */
export default function InvestigationTimeline({ report }: { report: FairReport }) {
  if (report.trace.length === 0) return null;
  const tokens = report.input_tokens + report.output_tokens;
  return (
    <details>
      <summary className="cursor-pointer text-sm font-medium text-slate-700">
        How the agent investigated ({report.trace.length} steps)
      </summary>
      <ol className="mt-3 space-y-2 border-l border-slate-200 pl-4">
        {report.trace.map((t) => {
          const kind = KIND[t.kind] ?? KIND.thought;
          return (
            <li key={t.step} className="text-sm">
              <span className={`mr-2 inline-block rounded border px-1.5 text-xs font-medium ${kind.style}`}>
                {kind.label}
              </span>
              {t.tool && <code className="mr-2 text-xs text-slate-500">{t.tool}</code>}
              <span className="text-slate-700">{t.summary}</span>
            </li>
          );
        })}
      </ol>
      <p className="mt-2 text-xs text-slate-500">
        {report.llm_calls} model calls · {report.tool_calls} tool calls · {tokens.toLocaleString()} tokens
        {report.paced_seconds > 0 && ` · waited ${Math.round(report.paced_seconds)}s for the rate limit`}
      </p>
      {report.notes.length > 0 && (
        <ul className="mt-2 list-disc pl-5 text-xs text-slate-600">
          {report.notes.map((n) => <li key={n}>{n}</li>)}
        </ul>
      )}
    </details>
  );
}
