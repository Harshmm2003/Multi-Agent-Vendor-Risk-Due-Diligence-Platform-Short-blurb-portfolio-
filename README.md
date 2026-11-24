# Third-Party Vendor Risk Assessment AI

A multi-agent system that runs vendor due diligence. You give it a vendor's name and a short profile of your own company. It gathers public data on the vendor, has five specialist AI agents score it, and returns a recommendation of **APPROVE**, **REVIEW** or **REJECT**. Every score comes with the evidence and reasoning behind it.

In the code and the UI, the project is called **VendorIQ**.

## Live demo

**[vendoriq-j2vo.onrender.com](https://vendoriq-j2vo.onrender.com)**

- **History** has full saved reports, including CVS Health evaluating Veeva Systems with the FAIR financial-exposure analysis. They open instantly.
- **Evaluate** runs a live evaluation of any public company. Visitors share **3 runs a day**, because the free AI key covers about five. Each run takes 3–8 minutes.
- It runs on free hosting, so the first visit after a quiet spell can take about a minute while the server wakes up.
- API docs: [vendoriq-api-d18l.onrender.com/docs](https://vendoriq-api-d18l.onrender.com/docs)

> **Decision support, not a decision.** The output is a starting point for a procurement or compliance reviewer. It is not legal, financial or compliance advice. Sanctions results in particular must be confirmed by a person before you act on them.

---

## Contents

- [Live demo](#live-demo)
- [What it does](#what-it-does)
- [How an evaluation works](#how-an-evaluation-works)
- [The five agents](#the-five-agents)
- [Financial exposure (FAIR agent)](#financial-exposure-fair-agent)
- [How the recommendation is decided](#how-the-recommendation-is-decided)
- [Certification evidence tiers](#certification-evidence-tiers)
- [Tech stack](#tech-stack)
- [Repository layout](#repository-layout)
- [Running it](#running-it)
- [Configuration](#configuration)
- [API](#api)
- [Development workflow](#development-workflow)
- [Known limitations](#known-limitations)
- [Roadmap](#roadmap)

---

## What it does

Vendor onboarding usually means one analyst checking a vendor's finances, certifications, sanctions status, news coverage and fit with the business, then writing a memo. This project automates the first pass:

- **Collects data** from public sources: Yahoo Finance, Wikipedia, SEC EDGAR, the FedRAMP marketplace dataset, the OFAC sanctions list and news feeds.
- **Screens for sanctions** against the U.S. Treasury OFAC SDN list. This is a rule-based check, not an AI judgement.
- **Verifies certifications** such as SOC 2, ISO 27001 and FedRAMP, and grades each one by how strong the evidence is.
- **Scores the vendor** in five areas, each handled by its own LLM agent.
- **Combines the scores** using weights you can adjust. Hard rules (sanctions, dealbreakers, missing data) override the scores.
- **Explains the result.** Each agent's score, confidence, red flags and cited evidence appear in a report.

## How an evaluation works

An evaluation runs as a background job. The frontend submits the request, then polls the job for progress until the result is ready.

```mermaid
flowchart TD
    A[POST /api/v1/evaluate<br/>vendor + client profile + weights] --> B[Job created<br/>returns job_id]
    B --> C[Scrape financials<br/>yfinance · Wikipedia · news]
    C --> D[OFAC SDN screening<br/>deterministic name matching]
    D --> E{Cert cache hit?<br/>Redis → Postgres}
    E -- no --> F[Certification discovery<br/>SEC EDGAR → FedRAMP → CSA STAR → web]
    E -- yes --> G
    F --> G[Build agent context<br/>+ RAG: industry benchmarks,<br/>compliance requirements, past evaluations]
    G --> H1[Financial agent]
    G --> H2[Compliance agent]
    G --> H3[Operational agent]
    G --> H4[Reputation agent]
    G --> H5[Strategic fit agent]
    H1 & H2 & H3 & H4 & H5 --> I[Meta-agent<br/>hard rules + weighted composite]
    I --> J[APPROVE / REVIEW / REJECT<br/>saved to Postgres + vector store]
```

The specialist agents run one after another, which keeps the system within the LLM provider's per-minute token limits. Each agent makes two calls:

1. **A reasoning pass** that writes free-text analysis citing the data it was given.
2. **A scoring pass** that returns structured JSON (`score`, `confidence`, `red_flags`, `evidence`, `explanation`). The JSON is requested with a schema, then validated again in code. See [ADR 0002](docs/adr/0002-structured-agent-output.md).

If an agent fails or returns something unusable, it is marked **unavailable**, not scored as zero. Its weight is shared among the agents that did report, and the evaluation is sent to human review.

## The five agents

| Agent | Default weight | What it looks at |
|---|---|---|
| **Financial** | 30% | Revenue and growth, liquidity (current ratio), leverage, interest coverage, years in operation |
| **Compliance & Legal** | 25% | Certifications against the ones the vendor's industry requires, regulatory disclosures, OFAC result, legal news |
| **Operational** | 20% | Company size and scale, operating history, ability to deliver at the contract's size |
| **Reputation** | 10% | Recent news coverage and red-flag stories |
| **Strategic Fit** | 15% | Match against *your* priorities, dealbreakers, nice-to-haves, risk appetite and contract size |

You can change the weights for each evaluation. They must cover exactly these five keys and add up to 1.

Alongside the composite score, the report shows two summary scores:

- **Risk score** = Financial × 0.55 + Compliance × 0.45. Lower means more risk.
- **Fit score** = Strategic × 0.50 + Operational × 0.35 + Reputation × 0.15.

Agents also draw on a small vector store (ChromaDB) that holds industry benchmarks, compliance requirements by industry, and summaries of past evaluations. This gives them a baseline to compare against.

## Financial exposure (FAIR agent)

After the five agents, a sixth, **agentic** analyst quantifies what the vendor
could cost you **in dollars per year**, using the FAIR model (Factor Analysis of
Information Risk). It models the scenarios that apply (data breach, outage,
insolvency, sanctions exit), runs a 10,000-year Monte Carlo simulation, and
reports the expected annual loss, the P50/P90/P99 losses, the chance of
reaching your tolerance, and a loss exceedance curve.

**How it works.** The agent plans its own investigation and calls tools:
cited baselines, the other agents' findings, financials and Altman Z, SEC
filings (cyber incidents, going concern), the simulation, sensitivity
analysis, and what-if contract controls (share fewer records, client-held
encryption, a fallback service, an exit plan). Code checks its report before
accepting it:

- every dollar figure must come from a simulation run (the model cannot invent one);
- every input cites its source (evidence, a dated benchmark such as IBM Cost of a
  Data Breach or S&P default rates, or a labelled assumption);
- the same evidence cannot be counted twice (for example lowering both how often
  a breach happens and how likely it is to succeed);
- a budget caps model calls, tool calls and tokens; time spent waiting on the
  provider's rate limit is not counted. If it cannot finish, a deterministic
  benchmark baseline is reported instead.

A tail-risk note is written by code, not the model, whenever the 1-in-100-year
loss exceeds your tolerance, so the wording of the narrative can never hide it.

**What you can tell it** (all optional, under "Financial exposure details" on
the form): data types shared (`pii`, `phi`, `payment`, `internal`), records
shared, service criticality, revenue at risk per hour, your dollar risk
tolerance, the vendor's liability cap and your cyber insurance. Without them it
uses benchmarks and derives a tolerance from your risk appetite and contract
value, and says so.

**How it affects the decision.** The meta-agent applies the result **after**
its rules and can only make the decision stricter:

| FAIR finding | Effect |
|---|---|
| Expected annual loss reaches the tolerance | REVIEW |
| 10%+ yearly chance of losses reaching the tolerance | REVIEW |
| 1-in-100-year loss, less liability cap and insurance, exceeds the tolerance | REVIEW |
| Expected annual loss exceeds the annual contract value | REVIEW |
| Typical-year (P50) loss reaches a tolerance **you stated**, confidence ≥ 50% | REJECT |

A contract control counts as a fix only if none of those findings would remain
with it in place; it then becomes a condition ("acceptable if…") instead of a
REJECT. Reports that are partial or mostly assumptions are shown but never
change the decision.

**Tested live** (Groq `gpt-oss-120b`, free tier):

| Client / vendor | FAIR result | Decision |
|---|---|---|
| Example healthcare client / example SaaS (standalone CLI) | $152K expected per year; 4 model calls, 15.5K tokens | within tolerance |
| Healthcare client, $250K tolerance / Veeva Systems | $305K expected, $7.75M of a 1-in-100 loss uncovered; 6 calls, 29K tokens | REVIEW, three FAIR reasons |
| CVS Health, $1M tolerance / Veeva Systems | $490K expected, P99 $9.41M (covered), 9.9% chance of reaching $1M; 8 calls, 43K tokens | REVIEW (from the score); FAIR within limits |

A full evaluation takes about 5–9 minutes on the free tier, mostly waiting on
its 8,000 tokens-per-minute limit, and uses about 60–90K of the 200K daily tokens.

It also runs standalone: `cd backend && uv run python -m fair analyze
fair/examples/analyze_healthcare_vendor.json` (`--offline` for no model). See
[ADR 0005](docs/adr/0005-fair-agent.md) for the design, the guardrails and
measured token use.

## How the recommendation is decided

The meta-agent (`backend/app/agents/meta_agent.py`) applies rules in a fixed order. The first rule that matches decides the outcome. **The LLM cannot override the hard rules.**

| # | Condition | Result |
|---|---|---|
| 1 | Confirmed OFAC sanctions match | **REJECT** |
| 2 | A dealbreaker is definitely violated (e.g. "must have SOC 2 Type 2" and it isn't there) | **REJECT** |
| 3 | Possible OFAC match (name overlap, or a name too short or generic to confirm) | REVIEW |
| 4 | OFAC screening couldn't be completed (list unreachable) | REVIEW |
| 5 | A dealbreaker can't be confirmed either way | REVIEW |
| 6 | Any agent unavailable | REVIEW |
| 7 | Any agent's output was truncated | REVIEW |
| 8 | Composite < 45 **and** average confidence ≥ 50% | **REJECT** |
| 9 | Composite < 45 but confidence < 50% (the low score may just mean missing data) | REVIEW |
| 10 | Average confidence < 95% | REVIEW |
| 11 | Composite ≥ 70 **and** confidence ≥ 95% | **APPROVE** |
| 12 | Everything else (composite 45–70) | REVIEW |

The system leans toward REVIEW on purpose. An automated APPROVE needs a strong score *and* very high confidence, because a wrong approval costs more than an extra human check.

## Certification evidence tiers

Every certification found is labelled with how much the evidence can be trusted:

| Tier | Meaning | Typical source |
|---|---|---|
| `VERIFIED_REGISTRY` | Confirmed in an official registry or regulatory filing | SEC EDGAR filings, FedRAMP "Authorized", CSA STAR Level 2 |
| `CORROBORATED` | Supported by a credible but weaker source | FedRAMP "Ready", CSA STAR Level 1, the vendor's own trust/compliance pages |
| `SELF_REPORTED_UNVERIFIABLE` | A claim that couldn't be checked | Reserved for future sources |
| `CONTRADICTED` | Evidence suggests the claim is false | Reserved for future sources |

When a dealbreaker names a certification, the meta-agent uses these tiers to decide whether it is met. For SOC 2, a dealbreaker that requires a Type 2 report is only met by Type 2.

Regulations such as GDPR, CCPA, SOX and FCPA are listed separately under `regulatory_frameworks_disclosed`. They are laws a company is subject to, not certifications it earns.

Discovery checks sources in order: **SEC EDGAR → FedRAMP → CSA STAR → the vendor's own website**. The website step is skipped when two or more registry-verified certifications have already been found. Results are cached for 7 days in Redis, backed by Postgres.

## Tech stack

| Layer | Technology |
|---|---|
| LLM | [Groq](https://console.groq.com) (`openai/gpt-oss-120b`) via LangChain |
| Backend API | Python 3.11, FastAPI, Uvicorn, async SQLAlchemy |
| Database | PostgreSQL 16 (local Docker or Supabase), Alembic migrations |
| Cache | Redis 7. If Redis is down, the app falls back to Postgres |
| Vector store | ChromaDB (embedded, in-process) |
| Frontend | React 18, TypeScript, Vite, Tailwind CSS, React Router |
| Tests | pytest (backend), Vitest + Testing Library (frontend) |
| Tooling | uv, ruff, mypy, GitHub Actions, gitleaks, Trivy, pip-audit |
| Runtime | Docker Compose (frontend, backend, Postgres, Redis) |

## Repository layout

```
.
├── backend/
│   ├── main_api.py              FastAPI app: middleware, CORS, health, error handling
│   ├── app/
│   │   ├── api/routes.py        REST endpoints + the evaluation pipeline (background job)
│   │   ├── agents/              Five specialist agents, the meta-agent, structured-output schema
│   │   ├── services/
│   │   │   ├── scraper.py             Financial data, news, industry
│   │   │   ├── ofac_screening.py      OFAC SDN download + name matching
│   │   │   ├── name_matching.py       Whole-token company-name matching
│   │   │   ├── compliance_discovery.py  Certification discovery entry point
│   │   │   ├── edgar_pipeline.py      SEC EDGAR filings
│   │   │   ├── fedramp_connector.py   FedRAMP marketplace data
│   │   │   ├── csa_star_connector.py  CSA STAR registry (demo dataset by default)
│   │   │   ├── cert_pipeline.py / cert_*_agent.py  Vendor website crawl + LLM extraction
│   │   │   ├── rag_service.py         ChromaDB collections and retrieval
│   │   │   └── cache.py               Redis layer
│   │   ├── db/                  SQLAlchemy models (evaluations, jobs, cert_cache) + engine
│   │   ├── llm/config.py        Groq model setup
│   │   ├── orchestrator/state.py  Shared evaluation state
│   │   └── settings.py          Environment-variable validation
│   ├── migrations/              Alembic migrations
│   ├── scripts/lint_baseline.py Lint ratchet (only new findings fail)
│   └── tests/                   pytest suite (offline by default)
├── frontend/
│   └── src/
│       ├── pages/               Evaluate, Job progress, History, Report
│       ├── components/          Score summary, OFAC banner, cert tiers, red flags, weights editor…
│       └── api/                 Typed API client
├── docs/
│   ├── IMPLEMENTATION_PLAN.md   Phased roadmap
│   └── adr/                     Architecture Decision Records
├── .github/workflows/ci.yml     Lint, tests, build, security scans
├── .githooks/                   pre-commit hook (lint + secret scan)
├── docker-compose.yml
└── Makefile                     Developer commands (`make help`)
```

## Running it

### Prerequisites

- A free **Groq API key** from [console.groq.com](https://console.groq.com). Evaluations need one; the rest of the app runs without it.
- **Option A:** Docker with Docker Compose.
- **Option B:** Python 3.11+, [uv](https://docs.astral.sh/uv/), Node.js 22, plus a PostgreSQL database and (optionally) Redis.

### Option A — Docker (recommended)

```bash
git clone <this-repo-url>
cd <repo-folder>
make up        # first run creates backend/.env from the example
```

Open `backend/.env`, set `GROQ_API_KEY`, then run `make up` again. This starts the frontend, the backend (with interactive API docs at `/docs`), Postgres and Redis on your machine; `docker-compose.yml` lists the ports.

Database migrations run automatically when the backend container starts.

No `make`? Use `cp backend/.env.example backend/.env`, edit it, then `docker compose up --build`.

### Option B — Run the services yourself

```bash
# Backend
cd backend
cp .env.example .env          # set DATABASE_URL and GROQ_API_KEY
uv sync --locked
uv run alembic upgrade head
uv run uvicorn main_api:app --reload --port 8000

# Frontend (new terminal)
cd frontend
npm ci
npm run dev
```

Set `VITE_API_BASE_URL` to point the frontend at a different backend.

> On first start, ChromaDB downloads a ~80 MB embedding model, so the first run needs internet access.

### Option C — Host it for free

[`render.yaml`](render.yaml) deploys the frontend and backend to Render's free plan, with Supabase (free) as the database. On the hosted version:

- **Anyone can browse the saved reports** in [`backend/demo/reports.json`](backend/demo/reports.json) (CVS Health and Northwind Health evaluating Veeva and Salesforce). They are loaded into the database on every start.
- **Visitors can start 3 live evaluations a day** between them, because the free Groq key covers about five. The Evaluate page shows how many are left.
- **The owner's passcode is never limited.** Enter it on the Evaluate page.
- The free backend sleeps after 15 minutes idle, so the first visit after that takes about a minute.

Setup steps, and the options for letting more people run evaluations: [`docs/deploy.md`](docs/deploy.md).

## Configuration

All settings live in `backend/.env`. See [`backend/.env.example`](backend/.env.example) for the full annotated list.

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | yes | Postgres URL using the asyncpg driver, e.g. `postgresql+asyncpg://user:pass@host:5432/db` |
| `GROQ_API_KEY` | yes | LLM access |
| `DATABASE_SSL` | no | `require` (default, for Supabase) or `disable` (local Postgres) |
| `REDIS_URL` | no | Redis for the certification cache; the app works without it |
| `CHROMA_PATH` | no | Vector store directory (default `./chroma_db`) |
| `LOG_LEVEL` / `LOG_FORMAT` | no | `INFO` / `json` by default; `console` is easier to read locally |
| `AGENT_STRUCTURED_OUTPUT` | no | `true` (JSON schema, default) or `false` (text parsing) |
| `CORS_ALLOW_ORIGINS` | no | Allowed browser origins, comma-separated |
| `CERT_CACHE_VALID_AFTER` | no | Treat cached certification results older than this date as expired |
| `CSA_STAR_API_KEY` | no | Live CSA STAR API (unused while the CSA connector uses its seed dataset) |
| `EVALUATE_PASSCODE` | no | Passcode for live evaluations on a hosted demo; holders are never limited |
| `EVALUATE_DAILY_LIMIT` | no | Evaluations anyone may start per rolling 24 h; `0` (default) means none if a passcode is set, unlimited if not |

Never commit `.env`. CI scans for secrets with gitleaks.

## API

Interactive docs are at `/docs` when the backend is running. The main endpoints, all under `/api/v1`:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/evaluate` | Start an evaluation. Returns `job_id` and `evaluation_id`. On a locked demo, send the `X-Evaluate-Passcode` header |
| `GET` | `/access` | Whether visitors may start evaluations: passcode on, free runs left today |
| `GET` | `/jobs/{job_id}` | Job status, progress %, current step, and the result once complete |
| `GET` | `/jobs` | Recent jobs |
| `GET` | `/evaluations` | Past evaluations |
| `GET` | `/evaluations/{id}` | One evaluation |
| `GET` | `/evaluations/{id}/report` | Full report with each agent's reasoning and evidence |
| `GET` | `/companies/search` | Company name lookup for the search box |
| `GET` | `/rag/stats` | Vector store collection sizes |
| `GET` | `/health` | Database and Redis health |

Try it against the live backend (read-only, so it uses none of the daily runs):

```bash
curl https://vendoriq-api-d18l.onrender.com/api/v1/evaluations?limit=3     # saved evaluations
curl https://vendoriq-api-d18l.onrender.com/api/v1/access                  # live runs left today
```

To start an evaluation, `POST /api/v1/evaluate`:

```bash
curl -X POST https://vendoriq-api-d18l.onrender.com/api/v1/evaluate \
  -H "Content-Type: application/json" \
  -d '{
    "vendor_name": "Microsoft",
    "client_profile": {
      "company_name": "Acme Manufacturing Corp",
      "industry": "Manufacturing",
      "contract_value": 5000000,
      "contract_years": 3,
      "risk_appetite": "medium",
      "priorities": ["cloud infrastructure", "cybersecurity"],
      "dealbreakers": ["no SOC2 certification", "active OFAC sanctions"],
      "nice_to_have": ["ISO27001 certified"]
    }
  }'
```

Then poll `GET /api/v1/jobs/<job_id>` until `status` is `complete`, which takes 3–8 minutes on the free tier.

## Development workflow

```bash
make install   # backend + frontend dependencies, and enables the git hooks
make check     # lint + type check + all tests: must pass before every commit
make help      # every available command
```

- **Tests are offline.** They use SQLite, mocked HTTP and a temporary vector store; they never touch a real database or the real `chroma_db`. Tests that call real services are marked `live` and run only with `pytest -m live` and a real Groq key.
- **Lint is a ratchet.** Existing findings are recorded in `backend/lint-baseline.json`; only new ones fail.
- **Schema changes** go through Alembic migrations, never `create_all`. See [ADR 0001](docs/adr/0001-schema-migrations.md).
- **Branches and commits:** one branch per task (`phase-N/<topic>`), pull requests into a protected `main`, and Conventional Commit messages (`feat:`, `fix:`, `docs:`…).
- **CI** (`.github/workflows/ci.yml`) runs lint, backend tests (including migrations against real Postgres), the frontend type check, tests and build, and security scans (gitleaks, Trivy, pip-audit, npm audit).
- **Design decisions** are recorded in [`docs/adr/`](docs/adr/).

## Known limitations

Current limitations, stated plainly:

- **CSA STAR uses a curated demo dataset.** The live registry API needs a paid membership. These entries are marked "[demo dataset]" in the evidence text.
- **FedRAMP data** comes from the public 18F dataset, which can lag the live marketplace by days or weeks.
- **Web-scraped sources** (Yahoo Finance, Wikipedia, Macrotrends, news) can change format or block requests. When a source fails, its fields are left empty and the agents' confidence drops. Scores are not invented to fill the gap.
- **Macrotrends revenue parsing** can pick up a quarterly figure instead of the annual one, and doesn't convert its values from millions. Check revenue numbers against the source.
- **Industry fallback.** If the vendor's industry can't be found, the client's industry is used instead. That can check the vendor against the wrong set of required certifications.
- **First run needs internet** for ChromaDB's embedding model, including the first run of the test suite.
- **FAIR estimates vary between runs.** The model's reading of the evidence moves the breach estimate (one example ranged $169K–$387K across runs). Control effect sizes and outage frequency are labelled assumptions, and benchmarks need refreshing as new editions come out.
- **Single tenant.** There are no user accounts or organisation isolation yet (planned in Phase 1).

## Roadmap

The phased plan is in [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md):

| Phase | Focus |
|---|---|
| 0 | Foundations: migrations, structured output, CI, branch protection, git hooks |
| 1 | Multi-tenancy, authentication and data isolation |
| 2 | Evaluation harness for measuring prompt and model changes |
| 3 | Guardrails |
| 4 | Scalability (move from ChromaDB to pgvector) |
| 5 | Owned data sources |
| 6 | Observability, load testing and SLOs |
| 7 | Cloud deployment on free tiers |
| 8 | SaaS polish |

---

**Author:** Harsh Motiramani · [LinkedIn](https://www.linkedin.com/in/harsh-motiramani-608321201/) · [harshmotiramani.com](https://harshmotiramani.com)
