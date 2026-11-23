import { useState } from "react";
import type { FairDriver } from "../../api/types";
import { inputLabel } from "../../lib/fair";
import { formatPercent, formatUsd } from "../../lib/format";

interface Props {
  drivers: FairDriver[];
  /** The scenario's expected annual loss, drawn as a reference line. */
  current: number;
}

/**
 * A ranked range ("tornado") chart: for each input, the expected annual loss
 * with that input at its lowest and at its highest. Longer bar = this input's
 * uncertainty matters more. One series, so no legend; each bar is its own
 * hover/focus target; the table view carries every value.
 */
export default function DriversChart({ drivers, current }: Props) {
  const [active, setActive] = useState<string | null>(null);
  if (drivers.length === 0) {
    return <p className="text-sm text-slate-600">Every input is a fixed value, so nothing to rank.</p>;
  }
  const max = Math.max(current, ...drivers.map((d) => Math.max(d.low_ale, d.high_ale))) || 1;
  const pct = (v: number) => `${(Math.max(0, v) / max) * 100}%`;

  return (
    <figure className="space-y-2">
      <figcaption className="text-sm font-medium text-slate-800">What drives the expected annual loss</figcaption>
      <ul className="space-y-2" aria-label="Inputs ranked by influence">
        {drivers.map((d) => {
          const lo = Math.min(d.low_ale, d.high_ale);
          const hi = Math.max(d.low_ale, d.high_ale);
          const isActive = active === d.path;
          const description = `${inputLabel(d.path)}: expected loss ${formatUsd(d.low_ale)} at its lowest, ${formatUsd(d.high_ale)} at its highest`;
          return (
            <li key={d.path} className="grid grid-cols-[9rem_1fr_3rem] items-center gap-3 text-sm">
              <span className="truncate text-slate-700" title={inputLabel(d.path)}>
                {inputLabel(d.path)}
              </span>
              <div
                className="relative h-6 rounded outline-none focus-visible:ring-2 focus-visible:ring-slate-300"
                tabIndex={0}
                aria-label={description}
                onPointerEnter={() => setActive(d.path)}
                onPointerLeave={() => setActive(null)}
                onFocus={() => setActive(d.path)}
                onBlur={() => setActive(null)}
              >
                <div className="absolute inset-y-0 w-px bg-slate-400" style={{ left: pct(current) }} aria-hidden />
                <div
                  className={`absolute top-1.5 h-3 rounded ${isActive ? "bg-slate-500" : "bg-slate-600"}`}
                  style={{ left: pct(lo), width: `calc(${pct(hi)} - ${pct(lo)})`, minWidth: 2 }}
                  aria-hidden
                />
                {isActive && (
                  <div
                    role="status"
                    className="pointer-events-none absolute -top-9 z-10 whitespace-nowrap rounded-md border border-slate-200 bg-white px-2.5 py-1 text-xs shadow-sm"
                    style={{ left: `min(${pct(lo)}, 55%)` }}
                  >
                    <span className="font-semibold tabular-nums text-slate-900">
                      {formatUsd(d.low_ale)} – {formatUsd(d.high_ale)}
                    </span>{" "}
                    <span className="text-slate-600">expected loss across its range</span>
                  </div>
                )}
              </div>
              <span className="text-right text-xs tabular-nums text-slate-600">{formatPercent(d.share)}</span>
            </li>
          );
        })}
      </ul>
      <p className="text-xs text-slate-500">
        Bars span the expected annual loss with each input at its lowest and highest; the line marks today's
        estimate ({formatUsd(current)}). The percentage is that input's share of all the swing.
      </p>
      <details className="text-sm">
        <summary className="cursor-pointer text-slate-600">Show as table</summary>
        <table className="mt-2 w-full text-left text-sm tabular-nums">
          <thead>
            <tr className="text-slate-500">
              <th className="py-1 font-medium">Input</th>
              <th className="py-1 font-medium">At its lowest</th>
              <th className="py-1 font-medium">At its highest</th>
              <th className="py-1 font-medium">Share</th>
            </tr>
          </thead>
          <tbody>
            {drivers.map((d) => (
              <tr key={d.path} className="border-t border-slate-100">
                <td className="py-1">{inputLabel(d.path)}</td>
                <td className="py-1">{formatUsd(d.low_ale)}</td>
                <td className="py-1">{formatUsd(d.high_ale)}</td>
                <td className="py-1">{formatPercent(d.share)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </figure>
  );
}
