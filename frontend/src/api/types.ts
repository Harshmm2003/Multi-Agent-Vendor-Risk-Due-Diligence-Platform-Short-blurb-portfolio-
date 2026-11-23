// Types mirror the backend exactly — see backend/app/api/routes.py and
// backend/app/db/models.py. Fields the backend may send as null are typed
// as such; the UI must never treat a missing value as a "good" one.

export const AGENT_KEYS = ["financial", "compliance", "operational", "reputation", "strategic"] as const;
export type AgentKey = (typeof AGENT_KEYS)[number];

export type Weights = Record<AgentKey, number>;

export type Recommendation = "APPROVE" | "REVIEW" | "REJECT";

/** app/services/ofac_screening.py — null when absent (e.g. saved reports). */
export type OfacStatus = "clear" | "match" | "possible_match" | "unavailable";

export type EvidenceTier =
  | "VERIFIED_REGISTRY"
  | "CORROBORATED"
  | "SELF_REPORTED_UNVERIFIABLE"
  | "CONTRADICTED";

// ── POST /api/v1/evaluate ─────────────────────────────────

/** routes.ClientProfile — every field is required by the backend. */
export interface ClientProfile {
  company_name: string;
  industry: string;
  contract_value: number;
  contract_years: number;
  risk_appetite: string;
  priorities: string[];
  dealbreakers: string[];
  nice_to_have: string[];
  /** Optional facts for the FAIR agent; left out when not given. */
  exposure?: ExposureProfile;
}

/** routes.EvaluateRequest — weights must name all five agents and sum to 1. */
export interface EvaluateRequest {
  vendor_name: string;
  client_profile: ClientProfile;
  weights: Weights;
}

/** 202 Accepted body. */
export interface EvaluateAccepted {
  job_id: string;
  evaluation_id: string;
  vendor_name: string;
  status: "pending";
  message: string;
  poll_url: string;
}

/** FastAPI / pydantic 422 detail entry. */
export interface ValidationErrorItem {
  type: string;
  loc: (string | number)[];
  msg: string;
  input?: unknown;
  ctx?: Record<string, unknown>;
}

// ── GET /api/v1/jobs/{id} ─────────────────────────────────

export type JobStatus = "pending" | "running" | "complete" | "failed";

/** Job.to_status_dict (+ result when complete, + error when failed). */
export interface Job {
  job_id: string;
  vendor_name: string;
  status: JobStatus;
  current_step: string | null;
  progress: number;
  created_at: string;
  updated_at: string;
  result?: EvaluationResult | null;
  error?: string | null;
}

/** response_data built at the end of routes._run_evaluation_pipeline. */
export interface EvaluationResult extends FairDecision {
  evaluation_id: string;
  vendor_name: string;
  recommendation: Recommendation | null;
  composite_score: number | null;
  risk_score: number | null;
  fit_score: number | null;
  confidence: number | null;
  dealbreaker_status: "clear" | "needs_review" | "hard_reject" | null;
  ofac_status: OfacStatus | null;
  ofac_matches: string[];
  unavailable_agents: string[];
  /**
   * Agents whose output was truncated (forces REVIEW). Absent only on job
   * results stored before routes.py started returning it.
   */
  truncated_agents?: string[];
  agent_scores: Record<AgentKey, number | null>;
  red_flags: string[];
  final_explanation: string | null;
  certifications_found: string[];
  certification_evidence_tiers: Record<string, EvidenceTier | string>;
  regulatory_frameworks_disclosed: string[];
  cert_evidence: string[];
  edgar_cik: string | null;
  industry: string | null;
  /** Absent on results stored before the FAIR agent existed. */
  fair?: FairOutput | null;
}

// ── GET /api/v1/companies/search ──────────────────────────

export interface CompanyMatch {
  name: string;
  ticker: string;
  cik: string;
}

// ── GET /api/v1/evaluations ───────────────────────────────

/** Evaluation.to_dict */
export interface EvaluationSummary {
  id: string;
  vendor_name: string;
  company_name: string;
  evaluation_id: string;
  scores: Record<AgentKey | "composite" | "risk" | "fit", number | null>;
  confidence: number | null;
  recommendation: Recommendation | null;
  red_flags: string[] | null;
  fair?: { expected_annual_loss: number | null; prob_exceeds_tolerance: number | null };
  created_at: string;
}

// ── GET /api/v1/evaluations/{id}/report ───────────────────

export interface AgentReport {
  score: number | null;
  confidence: number | null;
  explanation: string | null;
  reasoning: string | null;
  evidence: string[];
  red_flags: string[];
}

export type AgentBreakdownEntry = AgentReport | { status: "not run" };

export interface EvaluationReport {
  evaluation_id: string;
  vendor_name: string;
  evaluated_for: string;
  created_at: string;
  recommendation: Recommendation | null;
  scores: {
    composite: number | null;
    risk: number | null;
    fit: number | null;
    confidence: number | null;
  };
  agent_breakdown: Record<AgentKey, AgentBreakdownEntry>;
  red_flags: string[] | null;
  full_explanation: string | null;
  weights_used: Partial<Weights> | null;
  fair?: FairOutput | null;
}

// ── FAIR agent (backend/fair, ADR 0005) ───────────────────

export const DATA_TYPES = ["none", "internal", "pii", "phi", "payment"] as const;
export type DataType = (typeof DATA_TYPES)[number];
export type Criticality = "low" | "medium" | "high" | "critical";

/** fair.context.ExposureProfile — every field optional. */
export interface ExposureProfile {
  data_types?: DataType[];
  records_shared?: number | null;
  service_criticality?: Criticality | null;
  revenue_at_risk_per_hour?: number | null;
  risk_tolerance_usd?: number | null;
  liability_cap_usd?: number | null;
  cyber_insurance_usd?: number | null;
}

export interface LossExceedancePoint {
  loss: number;
  /** Chance a year's loss is at least `loss`. */
  probability: number;
}

/** fair.agent.report.LossSummary — dollars per year. */
export interface LossSummary {
  ale_mean: number;
  p10: number;
  p50: number;
  p90: number;
  p95: number;
  p99: number;
  prob_any_loss: number;
  prob_exceeds_tolerance: number | null;
  loss_exceedance_curve: LossExceedancePoint[];
}

export interface FairInput {
  path: string;
  min: number;
  mode: number;
  max: number;
  source: string;
  rationale: string;
}

export interface FairDriver {
  path: string;
  low_ale: number;
  high_ale: number;
  swing: number;
  share: number;
}

export interface FairScenario {
  run_id: string;
  scenario: string;
  title: string;
  label: string;
  loss_event_frequency_mean: number;
  loss: LossSummary;
  breakdown: Record<string, number>;
  inputs: FairInput[];
  assumption_count: number;
  drivers: FairDriver[];
}

export interface FairControl {
  run_id: string;
  base_run_id: string;
  scenario: string;
  control_id: string;
  title: string;
  parameter: number | null;
  tested_by: "agent" | "code";
  recommended: boolean;
  ale_before: number;
  ale_after: number;
  p99_before: number;
  p99_after: number;
  combined_after: LossSummary;
}

export interface FairTraceStep {
  step: number;
  kind: "thought" | "tool_call" | "tool_error" | "guardrail" | "fallback" | "error";
  tool: string | null;
  summary: string;
}

export interface FairEvidence {
  id: string;
  kind: string;
  title: string;
  detail: string;
  citation: string;
}

export interface FairTolerance {
  value_usd: number;
  source: "client" | "derived";
  rationale: string;
}

/** fair.agent.report.FairReport (the fields the UI uses). */
export interface FairReport {
  subject: string;
  status: "complete" | "partial";
  generated_by: "agent" | "fallback";
  scenarios: FairScenario[];
  combined: LossSummary;
  tolerance: FairTolerance | null;
  input_count: number;
  assumption_count: number;
  assumption_share: number;
  missing_inputs: string[];
  sharpen: string[];
  controls: FairControl[];
  narrative: string;
  key_findings: string[];
  recommendations: string[];
  notes: string[];
  evidence: FairEvidence[];
  trace: FairTraceStep[];
  llm_calls: number;
  tool_calls: number;
  input_tokens: number;
  output_tokens: number;
  paced_seconds: number;
}

/** What the pipeline stores: a report, a skip notice, or nothing (FAIR off). */
export type FairOutput = FairReport | { skipped: string };

export function isFairReport(value: FairOutput | null | undefined): value is FairReport {
  return value != null && "scenarios" in value;
}

/** The meta-agent's use of the FAIR figures (app/agents/meta_agent.py). */
export interface FairDecision {
  base_recommendation?: Recommendation | null;
  escalated_by_fair?: boolean | null;
  fair_effect?: "none" | "review" | "reject" | null;
  fair_reasons?: string[];
  conditions?: string[];
  fair_note?: string | null;
}
