/** Plain-English names for FAIR inputs and the client facts that sharpen them. */

const INPUT_LABELS: Record<string, string> = {
  threat_event_frequency: "How often it happens",
  vulnerability: "Chance it becomes a loss",
  "primary_loss.response": "Response cost",
  "primary_loss.productivity": "Lost productivity",
  "primary_loss.replacement": "Replacement cost",
  secondary_probability: "Chance of follow-on losses",
  "secondary_loss.fines_judgments": "Fines and judgments",
  "secondary_loss.reputation": "Reputation damage",
  "secondary_loss.competitive_advantage": "Competitive harm",
};

export function inputLabel(path: string): string {
  return INPUT_LABELS[path] ?? path;
}

/** Inputs that are probabilities or rates, not dollars. */
export function isMoneyInput(path: string): boolean {
  return path.startsWith("primary_loss") || path.startsWith("secondary_loss.");
}

const FACT_LABELS: Record<string, string> = {
  risk_tolerance_usd: "your dollar risk tolerance",
  records_shared: "how many records the vendor will hold",
  data_types: "what kind of data you will share",
  service_criticality: "how critical the service is",
  revenue_at_risk_per_hour: "revenue at risk per hour of outage",
  annual_contract_value: "the annual contract value",
  liability_cap_usd: "the contract's liability cap",
  cyber_insurance_usd: "your cyber insurance cover",
};

export function factLabel(fact: string): string {
  return FACT_LABELS[fact] ?? fact;
}

export function scenarioLabel(scenario: string): string {
  return (
    {
      vendor_breach: "Data breach",
      vendor_insolvency: "Insolvency",
      vendor_outage: "Outage",
      sanctions_exit: "Sanctions exit",
    }[scenario] ?? scenario
  );
}
