# VendorIQ — Multi-Agent Vendor Risk & Due-Diligence Platform

> Five specialized AI agents independently investigate a vendor — financial, compliance, operational, reputation, and strategic fit — then a meta-agent fuses their findings into a single CPO-ready recommendation: **Approve, Review, or Reject**, with evidence, confidence, and red flags.

Built on **IBM watsonx** (Llama-3.3-70B via `ChatWatsonx`) for the IBM watsonx challenge.

---

## What it does

Enterprise vendor due diligence is slow, manual, and inconsistent. VendorIQ automates the first pass: given just a vendor name, it scrapes real public data, runs five domain experts in parallel, and returns an auditable decision a Chief Procurement Officer can act on.

**The pipeline:**

1. **Scrape** — Pulls live data from Yahoo Finance (`yfinance`), Wikipedia, Macrotrends, OFAC sanctions lists, certification trust pages, and news sources.
2. **Five domain agents** — Each runs a two-pass routine: a chain-of-thought reasoning pass that cites every figure to its source, then a structured JSON scoring pass (0–100 + confidence + evidence + red flags).
   - **Financial** — liquidity, solvency, profitability, revenue stability, credit rating
   - **Compliance** — certifications (SOC2/ISO27001), OFAC sanctions, regulatory actions
   - **Operational** — scale, continuity, delivery capability
   - **Reputation** — news signals, track record, certifications
   - **Strategic** — fit against the client's priorities, dealbreakers, and nice-to-haves
3. **Meta-agent** — Applies company-defined weights (and redistributes them if an agent is missing), computes a composite score plus separate **Risk** and **Strategic-Fit** scores, enforces hard overrides (an OFAC match is an automatic reject), gates on a 95% confidence threshold for auto-approval, and writes a plain-English explanation with concrete action items.

---

## Engineering highlights

- **Two-pass agent design** — Reasoning is separated from scoring so every number in the final JSON traces back to a cited source, not a hallucination.
- **Source-grounded evidence** — Each data point carries its origin (`current_ratio: 1.4 [Source: Yahoo Finance]`) for a fully auditable trail.
- **Resilient JSON parsing** — Agent output is fence-stripped and brace-extracted, with a safe fallback object so one malformed response never crashes the pipeline.
- **Graceful degradation** — Missing agents have their weight redistributed across the rest; the decision still completes.
- **Hard-rule overrides** — Deterministic guardrails (OFAC, confidence floor) sit on top of the LLM scores so critical compliance rules can't be reasoned away.
- **Deterministic mode** — `temperature=0` + greedy decoding for reproducible, structured outputs.

---

## Tech stack

`Python` · `IBM watsonx` · `LangChain (langchain-ibm / langchain-core)` · `Llama-3.3-70B` · `yfinance` · `BeautifulSoup` · `asyncio`

---

## Architecture

```
                         ┌──────────────────┐
   vendor name  ─────▶   │     Scraper      │  yfinance · Wikipedia
                         │  (live sources)  │  Macrotrends · OFAC · news
                         └────────┬─────────┘
                                  │  builds VendorRiskState
        ┌────────────┬────────────┼────────────┬────────────┐
        ▼            ▼            ▼            ▼            ▼
   ┌─────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
   │Financial│ │Compliance│ │Operational│ │Reputation│ │Strategic │
   └────┬────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘
        └────────────┴────────────┼────────────┴────────────┘
                                  ▼
                         ┌──────────────────┐
                         │    Meta-Agent    │  weights · risk/fit
                         │  (synthesis)     │  OFAC override · gating
                         └────────┬─────────┘
                                  ▼
                  APPROVE / REVIEW / REJECT  + evidence + action items
```

---

## Getting started

```bash
# 1. Clone
git clone https://github.com/<your-username>/vendoriq.git
cd vendoriq

# 2. Install
pip install -r requirements.txt

# 3. Configure credentials
cp backend/.env.example backend/.env
#   then edit backend/.env with your IBM watsonx API key + project ID

# 4. Run the full pipeline (from the backend directory)
cd backend
python test_all_agents.py
```

To evaluate a different vendor, change the `VENDOR_NAME` and `CLIENT_PROFILE` at the top of `backend/test_all_agents.py`.

> **Requirements:** an active IBM watsonx project with access to `meta-llama/llama-3-3-70b-instruct`, plus outbound internet for the scrapers.

---

## Project structure

```
vendoriq/
├── backend/
│   ├── app/
│   │   ├── agents/          # 5 domain agents + base class + meta-agent
│   │   ├── orchestrator/    # VendorRiskState schema
│   │   ├── services/        # scraper.py (live data collection)
│   │   └── config_watsonx.py
│   ├── test_all_agents.py   # full end-to-end pipeline runner
│   ├── test_financial_agent.py
│   └── .env.example
├── requirements.txt
├── LICENSE
└── README.md
```

---

## License

MIT — see [LICENSE](LICENSE).

Built by [Harsh Motiramani](https://harshmotiramani.com) · [LinkedIn](https://www.linkedin.com/in/harsh-motiramani-608321201/)
