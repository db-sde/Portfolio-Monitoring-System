# PortfolioIQ

A personal mutual fund portfolio analysis tool. Upload a parsed CAS JSON
(the output of `casparser`, see the sibling [`casparser-web`](../README.md)
project), and it enriches every fund with live market data, computes
XIRR/returns/risk metrics, and gives you a dashboard broken down by
group / investor / advisor.

```
backend/    FastAPI app — calculations, enrichment, config, all routes
frontend/   React + Vite + Tailwind dashboard
```

## Architecture

Per the spec this was built from: the **backend runs locally** (your own
machine), not on a cloud host, with an optional `ngrok` tunnel if you want
to reach it from the Vercel-hosted frontend when you're not on the same
network. This isn't a stateless per-request tool like casparser-web —
it deliberately **persists** your last uploaded statement (`cas_data.json`)
and a 24h enrichment cache (`enrichment_cache.json`) to disk between runs,
since the whole point is revisiting your own portfolio over time. Both
are gitignored; they're your data, not something that belongs in the repo.

## The enrichment reachability caveat (read this first)

Three external data sources feed the "enriched" fields (spec section 7):

| Source | What it's for | Status while building this |
|---|---|---|
| **mfdata.in** | AUM, cap-allocation %, benchmark, category, expense ratio, fund manager, trailing returns, risk ratios (sharpe/alpha/beta/std dev) — almost everything | **Unreachable from every network path tried** while building this (direct request, docs page, different User-Agents — all failed, not with a 404 but a flat connection failure/403 consistent with bot-protection on datacenter IPs) |
| **mfapi.in** | NAV history + basic category | Confirmed working reliably |
| **captnemo** (Kuvera) | Category + scheme rules, by ISIN | Confirmed working, but doesn't carry cap-allocation/risk data at all |

Since this runs on **your own machine** rather than a cloud host, mfdata.in
may well work fine for you — the kind of protection that blocked every
attempt here usually isn't aimed at residential IPs. Test it yourself:

```bash
curl https://mfdata.in/api/v1/schemes/117560
```

If that returns real JSON, enrichment will pick up cap-allocation, risk
ratios, and trailing returns automatically — nothing to change. If it
doesn't, the app still works: those specific fields show as empty/"—"
rather than breaking anything (`enrichment.py`'s whole design is to
degrade field-by-field, never to fail the request), and you still get
real NAV history (for the snapshot's opening/closing balance math) and
category from the two working fallbacks.

`enrichment.py`'s field-name mapping for mfdata.in's response is taken
from the spec's own documented schema, not from a live response this
build actually saw — if your real responses use different field names,
`_extract_mfdata_fields()` in that file is the one place to adjust.

## Known simplifications vs. the full spec

- **Benchmark XIRR** (`benchmark_xirr`, `beating_benchmark` in the
  Portfolio Summary view) is always `null`. Computing it needs a real
  benchmark NAV history (e.g. Nifty 500 TRI) from a verified source —
  building that in speculatively felt worse than being honest that it's
  not there yet. `calculate_benchmark_xirr()` in `calculations.py` is
  fully implemented and unit-tested; it just needs a real data feed
  wired into `portfolio.py` to call it with.
- **Fund Summary** only lists funds you actually hold (`is_held: true`
  for everything). A fuller version comparing against funds you *don't*
  hold would need a broader fund catalog beyond what's in one statement.
- **Phase 2 endpoints** (`/api/portfolio/risk-reward`, `/api/portfolio/overlap`)
  aren't built — the spec marks these as Phase 2 explicitly.

## Running locally

```bash
# Backend
cd backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# Frontend (separate terminal)
cd frontend
npm install
npm run dev
# opens at http://localhost:5173
```

Upload your CAS JSON from the top bar. First upload kicks off enrichment
in the background — the top bar shows progress, and pages refresh once
it's done.

### config.json — attributing folios to a group/investor

Every distinct `advisor` (ARN code) your CAS shows folios under needs to
be listed in `config.json` to be attributed to a group/investor in the
Portfolio Summary view — an ARN not listed there just won't show up in
that one view (it's still visible everywhere else, unfiltered). Edit it
directly, or through the Settings page in the app.

### Exposing it via ngrok (optional)

```bash
ngrok http 8000
```

Take the `https://....ngrok.io` URL ngrok gives you and set it as
`VITE_API_BASE_URL` in the frontend's Vercel project (Environment
Variables → redeploy), and add that same ngrok URL to the backend's
`CORS_ORIGINS` env var (comma-separated) so the browser is allowed to
call it:

```bash
CORS_ORIGINS="http://localhost:5173,https://your-app.vercel.app" uvicorn main:app --port 8000
```

Note ngrok's free tier URL changes every time you restart the tunnel —
you'll need to update `VITE_API_BASE_URL` on Vercel each time unless
you're on a paid ngrok plan with a reserved domain.

## Endpoints

See spec section 6 for full request/response shapes. Quick reference:

- `POST /api/upload-cas` — upload a parsed CAS JSON, kicks off enrichment
- `GET /api/portfolio` — per-scheme data, filterable by level/group/investor/arn
- `GET /api/portfolio/snapshot` — opening/closing balance + XIRR for a date window
- `GET /api/portfolio/summary` — advisor-level comparison table
- `GET /api/portfolio/fund-summary` — returns heatmap for held funds
- `GET /api/portfolio/exposure` — top AMCs/funds + cap allocation
- `GET`/`POST /api/config` — read/write config.json
- `GET /api/enrich/status` — enrichment progress
- `GET /api/health` — liveness check
