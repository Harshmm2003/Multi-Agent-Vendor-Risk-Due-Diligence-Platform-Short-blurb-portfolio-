import { useId, useMemo, useRef, useState } from "react";
import type { LossExceedancePoint } from "../../api/types";
import { formatProbability, formatUsd } from "../../lib/format";

// One series, so no legend: the title names it. Colors are the app's slate
// ink (the same as the analysis score bars), a recessive hairline grid, and a
// neutral reference line for the tolerance; status colors stay reserved.
const INK = "#334155"; // slate-700: the curve
const GRID = "#e2e8f0"; // slate-200: hairline grid
const AXIS_TEXT = "#64748b"; // slate-500
const REFERENCE = "#64748b"; // slate-500: tolerance line
const SURFACE = "#ffffff";

const W = 640;
const H = 240;
const M = { top: 20, right: 16, bottom: 32, left: 48 };
const PLOT_W = W - M.left - M.right;
const PLOT_H = H - M.top - M.bottom;

interface Props {
  points: LossExceedancePoint[];
  tolerance?: number | null;
}

function niceCeiling(p: number): number {
  const steps = [0.01, 0.02, 0.05, 0.1, 0.2, 0.25, 0.5, 1];
  return steps.find((s) => s >= p) ?? 1;
}

export default function LossExceedanceChart({ points, tolerance }: Props) {
  const titleId = useId();
  const svgRef = useRef<SVGSVGElement>(null);
  const [active, setActive] = useState<number | null>(null);

  const geometry = useMemo(() => {
    const simulated = points.filter((p) => p.loss > 0);
    const smallest = simulated[0];
    if (!smallest) return null;
    // Below the smallest simulated loss, the chance of reaching an amount is
    // the chance of any loss: the curve is flat. Extend it down so a tolerance
    // under the smallest loss still has its reference line on the chart.
    const usable =
      tolerance && tolerance > 0 && tolerance < smallest.loss
        ? [{ loss: tolerance / 1.5, probability: smallest.probability }, ...simulated]
        : simulated;
    const first = usable[0]!;
    const last = usable[usable.length - 1]!;
    const lo = Math.log10(first.loss);
    const hi = Math.log10(last.loss);
    const span = hi - lo || 1;
    const yMax = niceCeiling(Math.max(...usable.map((p) => p.probability)));
    const x = (loss: number) => M.left + ((Math.log10(loss) - lo) / span) * PLOT_W;
    const y = (prob: number) => M.top + PLOT_H - (prob / yMax) * PLOT_H;
    const line = usable.map((p, i) => `${i ? "L" : "M"}${x(p.loss).toFixed(1)},${y(p.probability).toFixed(1)}`).join("");
    const area = `${line}L${x(last.loss).toFixed(1)},${y(0)}L${x(first.loss).toFixed(1)},${y(0)}Z`;
    // 1-2-5 ticks inside the range, thinned if crowded
    let xTicks: number[] = [];
    for (let e = Math.floor(lo); e <= Math.ceil(hi); e++)
      for (const m of [1, 2, 5]) {
        const t = m * 10 ** e;
        if (t >= first.loss && t <= last.loss) xTicks.push(t);
      }
    if (xTicks.length > 7) xTicks = xTicks.filter((t) => String(t).startsWith("1"));
    const yTicks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * yMax);
    const tolX = tolerance && tolerance >= first.loss && tolerance <= last.loss ? x(tolerance) : null;
    return { usable, x, y, line, area, xTicks, yTicks, tolX };
  }, [points, tolerance]);

  if (!geometry) {
    return <p className="text-sm text-slate-600">No simulated year produced a loss, so there is no curve to draw.</p>;
  }
  const { usable, x, y, line, area, xTicks, yTicks, tolX } = geometry;
  const point = active == null ? null : (usable[active] ?? null);

  const nearest = (clientX: number) => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0) return null;
    const px = ((clientX - rect.left) / rect.width) * W;
    let best = 0;
    let bestDistance = Infinity;
    usable.forEach((p, i) => {
      const distance = Math.abs(x(p.loss) - px);
      if (distance < bestDistance) {
        best = i;
        bestDistance = distance;
      }
    });
    return best;
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const step = e.key === "ArrowRight" ? 1 : -1;
    setActive((i) => Math.min(usable.length - 1, Math.max(0, (i ?? (step > 0 ? -1 : usable.length)) + step)));
  };

  const tableRows = usable.filter((_, i) => i % 5 === 0 || i === usable.length - 1);

  return (
    <figure className="space-y-2">
      <figcaption id={titleId} className="text-sm font-medium text-slate-800">
        Chance per year that losses reach at least a given amount
      </figcaption>
      <div className="relative max-w-[720px]">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${W} ${H}`}
          className="h-auto w-full touch-none outline-none focus-visible:ring-2 focus-visible:ring-slate-300"
          role="img"
          aria-labelledby={titleId}
          tabIndex={0}
          onKeyDown={onKey}
          onBlur={() => setActive(null)}
          onPointerMove={(e) => setActive(nearest(e.clientX))}
          onPointerLeave={() => setActive(null)}
        >
          {yTicks.map((t) => (
            <g key={t}>
              <line x1={M.left} x2={W - M.right} y1={y(t)} y2={y(t)} stroke={GRID} strokeWidth={1} />
              <text x={M.left - 6} y={y(t)} dy="0.32em" textAnchor="end" fontSize={11} fill={AXIS_TEXT}>
                {formatProbability(t)}
              </text>
            </g>
          ))}
          {xTicks.map((t) => (
            <text key={t} x={x(t)} y={H - M.bottom + 18} textAnchor="middle" fontSize={11} fill={AXIS_TEXT}>
              {formatUsd(t)}
            </text>
          ))}
          <path d={area} fill={INK} fillOpacity={0.1} />
          <path d={line} fill="none" stroke={INK} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
          {tolX != null && (
            <g>
              <line x1={tolX} x2={tolX} y1={M.top} y2={M.top + PLOT_H} stroke={REFERENCE} strokeWidth={1} />
              {/* Low in the plot, clear of the curve, with a surface halo over the wash */}
              <text
                x={tolX + 5}
                y={M.top + PLOT_H - 8}
                fontSize={11}
                fill={AXIS_TEXT}
                stroke={SURFACE}
                strokeWidth={3}
                paintOrder="stroke"
              >
                Tolerance {formatUsd(tolerance)}
              </text>
            </g>
          )}
          {point && (
            <g pointerEvents="none">
              <line x1={x(point.loss)} x2={x(point.loss)} y1={M.top} y2={M.top + PLOT_H} stroke="#94a3b8" strokeWidth={1} />
              <circle cx={x(point.loss)} cy={y(point.probability)} r={4} fill={INK} stroke={SURFACE} strokeWidth={2} />
            </g>
          )}
        </svg>
        {point && (
          <div
            role="status"
            className="pointer-events-none absolute top-1 rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-xs shadow-sm"
            style={{ left: `${Math.min(70, (x(point.loss) / W) * 100)}%` }}
          >
            <p className="font-semibold tabular-nums text-slate-900">{formatProbability(point.probability)} per year</p>
            <p className="text-slate-600">losses of at least {formatUsd(point.loss)}</p>
          </div>
        )}
      </div>
      <p className="text-xs text-slate-500">Dollar axis is logarithmic. Hover, or focus and use the arrow keys, to read values.</p>
      <details className="text-sm">
        <summary className="cursor-pointer text-slate-600">Show as table</summary>
        <table className="mt-2 w-full text-left text-sm tabular-nums">
          <thead>
            <tr className="text-slate-500">
              <th className="py-1 font-medium">Losses of at least</th>
              <th className="py-1 font-medium">Chance per year</th>
            </tr>
          </thead>
          <tbody>
            {tableRows.map((p) => (
              <tr key={p.loss} className="border-t border-slate-100">
                <td className="py-1">{formatUsd(p.loss)}</td>
                <td className="py-1">{formatProbability(p.probability)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </figure>
  );
}
