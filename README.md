# PortfolioIQ

A mutual-fund portfolio workspace with one database per installation. It imports CAMS/KFintech CAS PDFs or parsed CAS JSON, reconstructs transactions and FIFO lots, and provides dated valuations, XIRR, snapshots, advisor comparisons, fund analytics, exposures, and capital-gains exports. NSDL/CDSL demat statements are supported by the separate parser library but not by this portfolio application.

## Run locally

Use Python 3.13, Node 22+, and PostgreSQL. From this repository:

```sh
python3.13 -m venv .venv
.venv/bin/pip install --require-hashes -r backend/requirements.txt
cp backend/.env.example backend/.env
# Fill DATABASE_URL and APP_SECRET. ACCESS_MODE=local skips the app password.
# Keep COOKIE_SECURE=false for local HTTP.
.venv/bin/python backend/serve.py
```

In another terminal:

```sh
cd frontend
npm ci
npm run dev
```

Open the Vite URL. With `ACCESS_MODE=local`, the app opens without a login and the API accepts only loopback peers and local hostnames/origins. `serve.py` binds the API to `127.0.0.1` in this mode. Keep both servers on your machine; do not forward a public reverse proxy to local mode. The PDF password is still needed for encrypted statements. Vite proxies `/api` to port 8000. The frontend must reach the API through the same-origin proxy in production too; its Vercel rewrite is in `frontend/vercel.json`. Do not put credentials in frontend environment variables.

## Import and refresh lifecycle

The API validates upload size (20 MB maximum), encrypts the queued payload, and returns a durable job immediately. A separate worker parses in a resource-limited subprocess, resolves fund identities before opening the replacement transaction, then atomically replaces personal statement data. The previous statement remains readable until commit. Market histories, identity mappings and settings are reused.

Only one import or refresh can run at once. PostgreSQL locks coordinate workers; attempt numbers prevent resumed jobs' older workers from overwriting newer work. A job publishes `ready` with the dataset commit and remains active throughout progressive market enrichment. Reloading the page resumes polling. Cancellation takes effect at the next safe stage; a statement already committed remains available. Database reset is blocked while a job is active.

Encrypted queued payloads expire after one hour and are removed on completion, cancellation or failure. The PDF password is removed after parsing. Restarted workers resume from the latest committed stage, with at most three worker attempts. Keep APP_SECRET stable across processes and restarts. A stopped worker leaves jobs queued; `serve.py` supervises both processes and exits if either dies so the hosting platform can restart them.

Cached analytics remain visible while refresh runs. Outbound requests are deduplicated and bounded per host, with timeouts and Retry-After handling. A worker checks for stale or failed held-fund data and schedules refresh at most once every six hours after the last job; healthy cached analytics expire after 24 hours. Provider outages remain visible as partial/unavailable data.

## Calculation and data coverage

- Historical ownership and FIFO cost are rebuilt from the ledger through the requested date. Same-day statement order is preserved.
- A reversal cancels a uniquely matching, unconsumed purchase. Ambiguous reversals are flagged for review.
- Opening balances without acquisition history, gifted cost bases, segregated units, reconciliation failures and unresolved identities suppress affected cost/return calculations.
- Missing NAV means unavailable value, not zero or a fabricated loss. Totals include coverage and known value; exposure percentages use only valued holdings. Old NAV dates remain visible.
- Portfolio XIRR uses all scoped cash flows even when fully redeemed funds are hidden. Internal switch legs net when they fall on the same date within the selected scope; unmatched legs remain cash flows.
- Snapshot OTHER holdings are counted exactly once. Unknown valuations/cost coverage produce partial results.
- Capital-gains classifications reuse the bundled parser's existing tax engine. Unknown asset classifications or cost bases are excluded explicitly; an export with excluded disposals in the selected year is blocked. This change does not update statutory tax rules.
- Nifty 50 is an index-fund NAV proxy, not official TRI. Nifty 500, fund-specific benchmarks and market-cap allocations stay unavailable until reliable sources are configured.

## Deployment and upgrades

Build this repo with `docker build -t portfolioiq .`. The image installs the bundled local parser wheel and starts both API and worker. Configure DATABASE_URL, APP_SECRET, ACCESS_MODE=password, OWNER_PASSWORD, COOKIE_SECURE=true, and your CORS origin. Password mode is also the default when ACCESS_MODE is unset. Set an automatic restart policy. Alternatively run `uvicorn main:app` and `python worker.py` as separately supervised processes from `backend/`, sharing the same environment.

Each installation holds one shared portfolio. Multiple people can run separate password-free local installations, each with its own database. A hosted installation uses the owner password; everyone given that password sees and replaces the same portfolio. Separate private portfolios on one hosted app are not implemented. Remove old API_KEY/VITE_API_KEY configuration. Back up PostgreSQL before upgrading. Schema changes run transactionally under a migration lock. Existing imports should be re-uploaded once to rebuild ledger order, opening coverage and confirmed identities under calculation version 3.0.1; until then unverified values may be unavailable. Production data is never migrated by the test suite.

The parser wheel is versioned `1.3.0+portfolioiq.1`. Its source changes and regression test are included in `vendor/casparser-portfolioiq.patch`; the base upstream commit is documented in `vendor/README.md`. To rebuild after parser changes, bump its local version in the sibling `casparser` project and run:

```sh
uv build ../casparser --wheel --out-dir vendor
# Update the wheel path in backend/requirements.in, then regenerate the lock:
uv pip compile backend/requirements.in --python-version 3.13 --universal --generate-hashes -o backend/requirements.txt
```

## Tests

```sh
.venv/bin/pip install pytest
# Unit tests run offline. Integration tests require an explicitly named scratch database.
TEST_DATABASE_URL=postgresql://localhost/portfolioiq_test .venv/bin/python -m pytest backend/tests -q
npm --prefix frontend test
npm --prefix frontend run lint
npm --prefix frontend run build
```

The test configuration overrides DATABASE_URL before importing the app, refuses database names outside `portfolioiq_test*`, and blocks real provider calls. Integration tests delete scratch fixtures. Never point them at an application database. Parser tests live in the sibling parser repo; private encrypted-PDF fixtures are skipped when not provided.

See [implementation and validation notes](docs/performance-fixes.md) for the changes and measured limits.
