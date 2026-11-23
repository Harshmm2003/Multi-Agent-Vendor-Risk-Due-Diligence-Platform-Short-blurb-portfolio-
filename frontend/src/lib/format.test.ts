import { formatProbability, formatUsd } from "./format";

// The same cases as backend/tests/fair/test_fair_cli_and_boundary.py
// test_format_usd: a figure must read identically in the API and the UI.
test.each([
  [0, "$0"],
  [950, "$950"],
  [999.4, "$999"],
  [999.6, "$1K"],
  [12_345, "$12.3K"],
  [410_000, "$410K"],
  [999_400, "$999K"],
  [999_600, "$1M"],
  [1_250_000, "$1.25M"],
  [2_400_000, "$2.4M"],
  [3_000_000_000, "$3B"],
  [-50_000, "-$50K"],
])("formatUsd(%s) is %s, as in the backend", (value, text) => {
  expect(formatUsd(value)).toBe(text);
});

test("missing values never look like a number", () => {
  expect(formatUsd(null)).toBe("—");
  expect(formatUsd(Number.NaN)).toBe("—");
  expect(formatProbability(undefined)).toBe("—");
});

test("probabilities keep one decimal", () => {
  expect(formatProbability(0.029)).toBe("2.9%");
  expect(formatProbability(0.1)).toBe("10.0%");
});
