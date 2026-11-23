/** Scores are 0–100; null means the value wasn't produced. */
export function formatScore(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "—" : value.toFixed(1);
}

/** Confidence is 0–1. */
export function formatPercent(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "—" : `${Math.round(value * 100)}%`;
}

const USD_UNITS: [number, string][] = [
  [1e12, "T"],
  [1e9, "B"],
  [1e6, "M"],
  [1e3, "K"],
];

/**
 * Dollars exactly as the backend writes them (fair/format.py format_usd):
 * $950, $12.3K, $410K, $1.25M. The same figure must read the same everywhere.
 */
export function formatUsd(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const sign = value < 0 ? "-" : "";
  const amount = Math.abs(value);
  for (const [divisor, suffix] of USD_UNITS) {
    const scaled = amount / divisor;
    if (scaled >= 0.9995) return `${sign}$${Number(scaled.toPrecision(3))}${suffix}`;
  }
  return `${sign}$${Math.round(amount)}`;
}

/** Probabilities with one decimal, as the backend reports them: 2.9%. */
export function formatProbability(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "—" : `${(value * 100).toFixed(1)}%`;
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? iso
    : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
