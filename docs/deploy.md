# Hosting VendorIQ for free

Three free services; nothing here costs money.

| Part | Host | Free-plan behaviour |
|---|---|---|
| Frontend (React) | Render static site | Always on |
| Backend (FastAPI) | Render web service (Docker) | 512 MB RAM; sleeps after 15 min idle, ~1 min to wake |
| Database | Supabase Postgres | 500 MB; paused after a week with no activity |

Redis is optional: without it the app reads the cert cache from Postgres.

## Who may run live evaluations

Each evaluation spends about 30–45K Groq tokens, and the free Groq key allows
200K a day, so roughly four or five evaluations a day for everyone together.
Two settings on the backend decide who may start one:

| `EVALUATE_PASSCODE` | `EVALUATE_DAILY_LIMIT` | Result |
|---|---|---|
| unset | `0` | Anyone, unlimited (local development) |
| set | `0` | Passcode only: visitors browse saved reports; only the passcode runs live |
| set | `3` | **Hosted default** (`render.yaml`). Anyone may start 3 per rolling 24 h; passcode holders are never limited |
| unset | `3` | Anyone may start 3 per rolling 24 h |

The Evaluate page reads `GET /api/v1/access` and shows the matching notice
("locked on this demo" or "2 free evaluations left today") with a passcode
field. The passcode goes in the `X-Evaluate-Passcode` header and is compared in
constant time. Blocked requests get 403 (passcode) or 429 (allowance used up).

## Saved reports

A new database is empty, so the demo ships reports in
`backend/demo/reports.json`. The hosted container imports any that are missing
on every start (`scripts/demo_reports.py import`; existing ones are skipped).
To change the set, export from your local database and commit the file:

```bash
cd backend
uv run python scripts/demo_reports.py export eval-41995502 eval-e1f19ea4
```

Check the file before committing: it holds the client profile you typed in.

## Steps

1. **Supabase.** Create a project. Under *Connect*, copy the **Transaction
   pooler** URL and change `postgresql://` to `postgresql+asyncpg://`. The
   backend runs the migrations itself.
2. **Render.** *New → Blueprint*, pick this repo; `render.yaml` defines both
   services. Fill in what it asks for:
   - `GROQ_API_KEY`: your key
   - `DATABASE_URL`: the Supabase URL from step 1
   - `EVALUATE_PASSCODE`: a phrase only you know
   - `CORS_ALLOW_ORIGINS` and `VITE_API_BASE_URL`: the two service URLs
     Render shows (`https://vendoriq.onrender.com`, `https://vendoriq-api.onrender.com`).
     The frontend URL bakes in at build time, so if you change it, redeploy
     the frontend.
3. Open the frontend URL and check History lists the saved reports. Then run
   one evaluation with the passcode.

Secrets live only in Render's environment settings, never in the repo.

## Letting visitors run their own evaluations

The code needs no change; it is a question of tokens and time.

1. **A small public allowance (free, the hosted default).** `EVALUATE_DAILY_LIMIT=3`:
   visitors get three a day between them and you keep the rest with the
   passcode. If the limit is too high, the Groq key runs dry and later runs
   fall back to benchmark FAIR estimates and failing agent calls.
2. **A paid Groq tier (cheap).** Pay-as-you-go lifts the daily cap; at
   gpt-oss-120b prices an evaluation costs about a cent. Raise
   `FAIR_TOKENS_PER_MINUTE` to the new per-minute limit (or `0`) and runs
   drop from 3–8 minutes to about one. Set a spending limit in the Groq
   console, and keep a daily limit as a second guard.
3. **Each visitor's own key.** Accept a Groq key in the form and use it for
   that run only (never stored). This costs you nothing however many people
   use it, but it is a code change: the agents and the FAIR agent read the key
   from settings today.
4. **An always-on backend.** Render's paid instance (about $7/month) stops the
   idle sleep and adds memory; several runs at once need it. Free alternatives
   are a scheduled ping every 10 minutes (counts against Render's 750 free
   hours) or a free VM such as Oracle Cloud's.

Also worth adding before a wide launch: per-visitor limits (by IP or sign-in)
rather than one shared allowance, and a queue so a burst of runs does not
share the per-minute token limit at once.
