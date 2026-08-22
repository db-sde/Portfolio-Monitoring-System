# PortfolioIQ

A personal mutual fund portfolio analysis tool, and the product this
repo deploys. Upload your CAS PDF (CAMS or KFintech) and its password —
that's the whole input — and it parses it in-process via `casparser`
(the same library the retired [`casparser-web`](archive/casparser-web/README.md)
tool wraps, kept in `archive/` for reference, not deployed anymore),
reconstructs a real FIFO acquisition-lot ledger from the transaction
history, values everything against live MFAPI-resolved NAV (never the
CAS's own printed valuation), and gives you a dashboard broken down by
group / investor / advisor — current value, XIRR, capital gains,
benchmark comparison, risk ratios, all derived from that one ledger.

(A pre-parsed CAS JSON is still accepted too, if you already have one —
`/api/upload-cas` detects which by file extension. Only CAMS/KFintech
mutual-fund statements are analysed; an NSDL/CDSL demat statement is
rejected with a clear message, since the analytics here — XIRR, FIFO
capital gains, advisor comparison — are all built around the
folio/scheme/transaction shape those two RTAs produce.)

```
backend/            FastAPI app on Postgres — ingestion, FIFO/XIRR
                     engine, enrichment, benchmark simulation, all routes
frontend/            React + Vite + Tailwind dashboard
archive/casparser-web/  Retired — the PDF-parser-only tool this replaced
```

## Architecture

Every number on every page traces back to one of two things: the CAS
statement's own transaction ledger (never its printed valuation), or a
live NAV resolved from mfapi.in. Concretely:

- **Postgres (Neon)** is the only persistence layer — `backend/models.py`
  defines cas_uploads/folios/schemes/scheme_aliases/holdings/
  transactions/purchase_lots/disposal_allocations/nav_cache/
  enrichment_cache/benchmark_* /config_*, all Decimal-precision. There's
  no JSON-file storage left; a Render redeploy no longer wipes anything.
- **`fifo.py`** is the acquisition-lot engine (FIFO, at investor + folio +
  scheme + plan + option grain) that every valuation, gain, and days-held
  figure is built from — verified exactly against a worked multi-purchase
  /partial-redemption example. Gift-in/gift-out, segregation, and
  REVERSAL (a bounced/reversed SIP installment — the AMC claws back the
  exact units and amount of an earlier purchase; found on a real
  statement) reduce a holding's real unit balance without generating a
  taxable disposal (donor cost basis isn't available from a single CAS
  for gifts; a reversal was never a real disposal at all — see
  `fifo.py`'s `NON_TAXABLE_REDUCTION_TYPES`). REVERSAL wasn't handled at
  all until a real holding's derived balance came out 82 units higher
  than the CAS statement's own printed close — exactly the sum of that
  holding's three reversed SIP installments, which nothing had ever
  subtracted back out. The same REVERSAL gap turned out to be in two
  more places, found by auditing every spot in the backend that
  branches on transaction type: `portfolio_service.py`'s holding-level
  XIRR already special-cased it correctly, but `snapshot_service.py`'s
  own period cash-flow list and its displayed "Purchase" total didn't —
  a fully-reversed SIP would count as a real outflow with nothing to
  offset it, inflating both the Snapshot page's Purchase figure and
  understating its XIRR. Fixed the same way, in both places. Separately,
  the spec's own requirement that "tax cost includes apportioned
  acquisition stamp duty" had all its plumbing built
  (`PurchaseLot.stamp_duty`, `gains_service_db.py`'s proportional split
  on partial disposal) but `ingestion.py` never actually extracted the
  real amount from a CAS statement's own `STAMP_DUTY_TAX` rows — every
  lot's stamp duty silently stayed zero, understating cost basis (and
  overstating realized gains) on the 112A export. Now matched by
  same-holding + same-date and wired through.
- **`xirr_engine.py`** solves money-weighted XIRR from full dated
  cash-flow history — every page's XIRR (including subtotals/advisor
  blends) is recalculated from consolidated cash flows, never averaged
  from child XIRRs.
- **`scheme_resolution.py`** resolves a CAS scheme to a canonical record
  by ISIN first, then a validated AMFI code, then a persisted alias
  (this is what recovers a fund whose AMFI code was retired by an AMC
  merger — e.g. HSBC absorbing L&T's schemes in 2022 left an old code
  frozen on its last NAV while the real fund kept trading under a new
  one; confirmed live and fixed here).
- **`benchmark_service.py`** replays a holding/advisor/portfolio's own
  external cash flows into a benchmark's NAV series for a
  personal-XIRR-comparable benchmark return — see the caveat below on
  which benchmarks that's actually possible for.

**Upload performance**: `ingest_cas` resolves schemes sequentially (each
`resolve_scheme` call reads/writes the same DB session, so it can't
safely run concurrently with itself) — but the mfapi.in *network* call
each one makes is independent of the DB, so `scheme_resolution.py`'s
`prefetch_mfapi_schemes` fetches every scheme's mfapi.in data
concurrently up front, and the sequential resolution loop then reads
from that prefetched map instead of making its own live call. This
exists because of a real incident: a production upload was timing out
with a 500, traced by profiling the exact ingestion path end-to-end —
resolving 5 schemes sequentially spent ~35s just waiting on mfapi.in
(its own per-call latency is highly variable, 1-15s, especially under
concurrent load), and a separate N+1 query pattern in
`_persist_transactions` (one round trip per incoming transaction to
check for duplicates, instead of one per holding) added another ~75s
importing 120 transactions. Fixed, in order: batch the duplicate check
into one query per holding, then prefetch mfapi.in concurrently — took
a 5-fund/120-transaction import from 145s down to ~25s.

**Background enrichment reliability**: `/api/upload-cas` returns as soon
as ingestion/FIFO finish and kicks off NAV/risk-ratio enrichment as a
FastAPI `BackgroundTask` (`_run_enrichment_task` in `main.py`), so the
response doesn't wait on 10+ more mfapi.in round trips. A real upload of
14 real schemes hit an incident where every single one came back with
no NAV data (current value stuck at ₹0) even though re-running the
exact same enrichment call by hand against the same schemes moments
later succeeded completely — proving the enrichment logic itself was
correct. Root cause: `enrichment.py`'s `enrich_schemes` fetched the
whole batch through one `asyncio.gather(...)` with default settings, so
a single scheme's transient mfapi.in blip (confirmed elsewhere in this
doc to happen under concurrent load) raised, and plain `gather()`
discards every other coroutine's already-completed result the moment
any one of them raises — an all-or-nothing batch, not a per-scheme
failure. Compounding it, nothing wrapped `_run_enrichment_task` itself,
so the exception vanished with no trace: background tasks run after the
HTTP response is already sent, so there's no request/response cycle
left to surface an error through. Fixed three ways: `enrich_schemes`
now calls `gather(..., return_exceptions=True)` and logs+skips just the
scheme(s) that failed; `enrichment_bridge.refresh_enrichment`'s
per-scheme DB write runs inside its own `session.begin_nested()`
SAVEPOINT so one scheme's bad payload can't roll back every other
scheme already flushed in the same shared session; and
`_run_enrichment_task` is now wrapped in try/except with
`logger.exception`, so a future failure is visible in Render's log
viewer instead of only discoverable by directly querying the database.

## Deployment

- **Backend** → Render, building `Dockerfile` at this repo's root
- **Frontend** → Vercel, Root Directory `frontend`

**Postgres is required**, not optional — the backend won't start without
`DATABASE_URL`:

1. Create a free project at [neon.tech](https://neon.tech) and copy its
   connection string (Neon console → your project → **Connect** → the
   `psql`/pooled connection string, looks like
   `postgresql://<user>:<password>@<host>.neon.tech/<database>?sslmode=require`).
2. **Render** → this service → *Environment* → add an environment
   variable named `DATABASE_URL` with that value.
3. **Local dev**: `cp backend/.env.example backend/.env` and paste the
   same connection string in as `DATABASE_URL` — `main.py` loads `.env`
   automatically via `python-dotenv`. `.env` is gitignored; never commit
   it or paste a real connection string into chat/an issue/a PR.

Schema creation is idempotent and automatic (`db.init_db()` runs on every
startup) — there's no separate migration step to run by hand. Neon's
free-tier compute suspends after inactivity and wakes on the next
connection, so the very first request after a quiet period can take a
few seconds longer than usual; that's expected, not a failure.

**`API_KEY` is also required**, not optional — every request to the
backend (except `GET /api/health`) must send it back as an `X-API-Key`
header, checked in `main.py`'s `_require_api_key` middleware, or the
backend returns 401 (or 500 if `API_KEY` itself isn't set). This exists
because of a real incident: with this repo public on GitHub and zero
auth on any route, a real production upload's entire dataset — every
statement, holding, and transaction — was wiped via `DELETE
/api/all-data` by an unauthenticated caller; nothing in this app's own
code or UI could have done it. A shared key isn't real per-user auth
(it ships in the built frontend bundle, so it's visible to anyone who
inspects that bundle's own network requests), but it stops a stranger
or bot from calling the API directly without ever loading the app,
which is the exposure this incident actually came from.

1. Generate a key: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. **Render** → backend service → *Environment* → add `API_KEY` with
   that value.
3. **Vercel** → frontend project → *Environment Variables* → add
   `VITE_API_KEY` with the **same** value, then redeploy (Vite bakes env
   vars in at build time, so a var added after the last build won't take
   effect until the next one).
4. **Local dev**: put the same value in `backend/.env`'s `API_KEY` and
   `frontend/.env`'s `VITE_API_KEY` (copy each from its `.env.example`
   first) — they must match each other exactly, but don't need to match
   Render/Vercel's production value.

## Enrichment sources — what's real, what's genuinely unavailable

| Source | What it provides | Status |
|---|---|---|
| **mfapi.in** | NAV history (the basis for *everything* — current value, returns, volatility, Sharpe/Sortino/max-drawdown, and the Nifty 50 benchmark proxy series) | Confirmed working; retried a few times on failure since it's a free, best-effort API that fails transiently under load (confirmed live, not hypothetical) |
| **captnemo** (Kuvera's backing API) | Category, expense ratio, fund manager, a volatility figure, by ISIN | Confirmed working |
| **mfdata.in** | Was meant to be primary (cap-allocation %, precomputed ratios) | **Confirmed permanently unreachable** — Cloudflare returns error 522 (origin down), not a bot-block. Still probed once per scheme with a short timeout in case it ever comes back; nothing in the app waits on it succeeding |

Returns, volatility, Sharpe, Sortino, max drawdown, alpha, and beta are
all computed directly from mfapi.in's NAV history — no dedicated ratios
endpoint exists on any free source, so `enrichment.py` derives them with
standard formulas instead (cross-checked against an independent
NAV-analytics site's live figures for several real funds).

**Two gaps remain genuinely open**, not simplified-away:

- **Cap allocation** (large/mid/small-cap %) and **sector/holdings
  exposure** — no free source publishes real portfolio-holdings data.
  Every page that would show this (Dashboard, Exposure, Portfolio
  Summary) correctly reports "unavailable" rather than guessing from a
  scheme's category label.
- **Nifty 500 and fund-respective benchmark XIRR** — no free source
  publishes raw index values for either. The **Nifty 50** column *is*
  real: it uses a Nifty 50 index fund's own NAV as a proxy (clearly
  labelled as a proxy, never presented as the official TRI series), the
  same NAV-history mechanism as everything else. `benchmark_service.py`'s
  `BenchmarkProvider` interface and the `scheme_benchmark_map` table are
  ready for a real source the moment one exists — nothing here needs
  rewriting later, just a new provider wired in.

## Running it locally instead

```bash
# Backend
cd backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # paste in your Neon DATABASE_URL and an API_KEY (see Deployment above)
uvicorn main:app --reload --port 8000

# Frontend (separate terminal)
cd frontend
npm install
cp .env.example .env   # VITE_API_KEY must match the backend's API_KEY exactly
npm run dev
# opens at http://localhost:5173
```

Both `.env` files are required, not optional — the backend rejects every
request without a matching `API_KEY`/`VITE_API_KEY` pair (see
Deployment's `API_KEY` section above for why). Skipping either one shows
up as every request in the browser failing with 401.

Click "Upload CAS PDF" in the top bar, pick your statement, enter its
password when prompted, and hit Parse. First upload kicks off enrichment
in the background — the top bar shows progress, and pages refresh once
it's done. Uploading the same file again (byte-identical) is recognised
as a duplicate and skipped, not re-imported; uploading a *different*,
overlapping statement for the same folios merges in cleanly (transactions
are deduplicated by fingerprint, not by file).

### Groups, investors & advisors

Every distinct `advisor` (ARN code) your CAS shows folios under needs to
be attributed to a group/investor to show up correctly in the Portfolio
Summary view — do this through the Settings page in the app (persisted
in Postgres now, not a config.json file).

### Resetting all data

`DELETE /api/all-data` wipes every statement, holding, gain, cached
market-data point, and config entry — a full reset, not a per-statement
delete (holdings/lots can be shared across multiple accumulated CAS
uploads for the same folio, so a correct partial delete is a separate,
not-yet-built feature).

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

## Testing

```bash
cd backend
python3 tests/test_fifo.py              # FIFO lot engine, pure/offline
python3 tests/test_xirr.py              # XIRR solver, pure/offline
python3 tests/test_ingestion_helpers.py # ingestion.py's pure helper functions, pure/offline
python3 tests/test_integration.py       # needs DATABASE_URL — real Neon connection, TRUNCATEs tables
```

**`test_integration.py` is destructive** — its `_cleanup()` TRUNCATEs
cas_uploads/holdings/schemes/transactions/etc. with CASCADE at the start
and end of every test. This project has one Neon database, not a
separate test instance, and running this file with `DATABASE_URL`
pointed at a database holding a real upload will destroy it — a real
incident, not a hypothetical: it happened mid-session, wiping a just
re-uploaded real portfolio with zero warning. `_cleanup()` now refuses
to run if `cas_uploads` holds anything that isn't this file's own test
fixture data (every fixture uses investor name `"T"`; a real name
trips the guard), but that's a tripwire for this one specific mistake,
not a reason to skip checking what `DATABASE_URL` actually points at
before running this file.

28 tests total across the four files. Every bug fix described in
Architecture above shipped with a regression test in one of these —
`test_fifo.py`'s `test_reversal_nets_out_against_the_purchase_it_reverses`,
`test_integration.py`'s `test_stamp_duty_flows_into_lot_cost_basis` and
`test_snapshot_reversal_does_not_inflate_purchase_or_xirr`, and
`test_ingestion_helpers.py`'s `test_as_date_handles_dd_mon_yyyy` are the
direct record of each real incident, not just abstract coverage.

## Endpoints

Every one of these requires the `X-API-Key` header described in
Deployment's `API_KEY` section, except `GET /api/health`.

- `POST /api/upload-cas` — upload the CAS PDF + password (or a pre-parsed CAS JSON); ingests, dedupes, runs FIFO, kicks off enrichment in the background
- `GET /api/portfolio` — per-holding table + asset-class subtotals, filterable by level/group/investor/arn and valuation_date
- `GET /api/portfolio/snapshot` — opening/closing balance + net gain + XIRR for a date window, bucketed by asset class
- `GET /api/portfolio/summary` — advisor-level comparison, including Nifty 50 (proxy) / Nifty 500 / fund-respective benchmark XIRR columns
- `GET /api/portfolio/fund-summary` — returns/risk-ratio heatmap for held funds
- `GET /api/portfolio/exposure` — top AMCs/funds (live value) + cap allocation (honest "unavailable")
- `GET /api/transactions` — flat transaction ledger, filterable
- `GET /api/capital-gains` — realised gains for the selected FY + gift-transfer disclosure
- `GET /api/capital-gains/112a.csv?fy=` — official Schedule 112A export (casparser's own verified format)
- `GET /api/data-quality` — every holding currently flagged for review, with why
- `GET`/`POST /api/config` — read/write group/investor/advisor attribution + preferences
- `DELETE /api/all-data` — full data reset
- `GET /api/enrich/status` — enrichment progress
- `GET /api/health` — liveness check (no DB dependency)

Every response includes `requested_valuation_date`, `holdings_coverage_through`,
`nav_policy`, `calculation_version`, `warnings`, and `data_quality`
(`OK`/`PARTIAL`) metadata. A holding needing attention carries a `flags`
array with a specific code — `SCHEME_UNRESOLVED`, `NAV_UNAVAILABLE`,
`CAS_RECONCILIATION_FAILED`, `INCOMPLETE_OPENING_HISTORY`,
`FIFO_SHORTFALL`, or `XIRR_NO_SOLUTION` — never a silently wrong number.
