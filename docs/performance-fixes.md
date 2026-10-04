# Efficiency and reliability changes

Implemented across the PortfolioIQ app and the sibling casparser package. No production database was used for validation and these changes have not been deployed.

## What changed

| Area | Previous failure or unnecessary work | Implemented behavior |
| --- | --- | --- |
| Imports | Web-process background work could disappear on restart; completion was reported before enrichment finished | Durable PostgreSQL queue, supervised worker, heartbeat, resumable stages, cancellation, attempt fencing |
| Replacement | Repeated uploads discarded market histories and could race refresh/reset | Atomic personal-data replacement, retained reusable market caches, serialized mutations |
| Parser | All-page investor scan, retained page objects, repeated ISIN connections | First-page investor extraction, streaming pages with native handle cleanup, one lookup session per parse, subprocess resource bounds |
| Providers | Nested requests escaped concurrency limits; duplicate histories and cached failures slowed retry | Per-host request limit of eight, request deduplication, deadlines, Retry-After handling, retryable failures, progressive cache writes |
| Enrichment persistence | Retried metadata could lose NAV points after rollback | Whole NAV-and-metadata write is replayed; last-known usable analytics survive provider failure |
| Identity | Incorrect original AMFI history could survive recovery | ISIN-confirmed histories only; unverified legacy histories rebuilt on confirmation |
| Calculations | Historical requests used current lots; reversals consumed older purchases; OTHER could be counted twice | Dated ledger/FIFO, explicit reversal matching, separate OTHER bucket |
| Missing data | Missing NAV could look like zero value or a loss; aggregate XIRR bypassed coverage | Nullable values, coverage totals, shared eligibility checks, visible warnings and stale NAV dates |
| Reporting | Advisor and benchmark queries grew with holdings/cash flows; dashboard repeated calculations | Shared batched context, in-memory benchmark date lookup, combined dashboard response |
| Tables and settings | Entire transaction tables, out-of-order responses, lost save/export errors | Paginated transactions/gains/gifts, scoped totals, response guards, explicit errors, versioned settings and dirty-state checks |
| Frontend startup | All pages and charts were in the initial bundle | Lazy page loading and bounded request cache |
| Access | App entry required an owner password | Direct access on local and hosted installations; no frontend secret or session gate |
| Delivery | Container installed a published parser instead of local changes; loose dependencies | Versioned local wheel, hashed dependency lock, supervised API/worker startup, isolated database CI |

## Validation

- Backend: **66 tests passed** on the locked Python 3.13 runtime with a disposable local PostgreSQL database. Provider calls were mocked/blocked. This covers real ingest/parse subprocess, job recovery and fencing, atomic rollback, cache retention and repair, password-free hosted access, settings conflicts, historical calculations, NAV absence, reversal ordering, financial-year export, pagination, and request concurrency.
- Parser: **176 passed; 68 skipped**. Skips require private PDF fixtures or passwords. New tests cover first-page extraction, lazy iteration, cleanup on exceptions, and ISIN-session reuse.
- Frontend: production build and lint passed. Browser smoke test verified direct access, dashboard values and quality warnings, transactions, and settings; no browser console errors were recorded.
- A 50-holding fixture confirms that loading the calculation context and calculating all holdings uses **five database queries**. Timing against a remote production database was not measured.
- Initial JavaScript: approximately **656 KB → 220 KB**, or **189 KB → 69 KB gzip**. Charts and page code are downloaded as separate chunks when needed. This is a startup-transfer improvement, not a claim that total application code shrank by the same amount.
- Local parser wheel installed from the hash-locked requirements; parser version verified as `1.3.0+portfolioiq.1`.

## Deployment requirements and remaining limits

### Calculation follow-up (3.0.1)

An imported statement exposed a same-day reversal printed before its matching purchase. FIFO now resolves unique same-day cancellation pairs before replaying disposals and retains ambiguity checks. Stamp-duty refunds no longer increase acquisition costs, and signed stamp duty is included consistently in XIRR and snapshot cash flows. Import-time FIFO warnings are recomputed for the requested valuation date.

Snapshot opening/closing values stay visible when only cost/return coverage is incomplete. Aggregate cost and coverage counts concern active holdings; lifetime XIRR retains closed-holding cash flows. The dashboard distinguishes remaining cost and unrealised gains from lifetime returns, provides a fully-redeemed-holdings toggle, and displays available asset-class allocation instead of an empty market-cap chart. Hybrid holdings retain their own bucket.

`ingestion.rebuild_derived_ledger(session)` transactionally rebuilds lots and disposal allocations from stored transactions under the import control lock. It refuses to run during an active job. Back up the database before invoking this maintenance function and commit using the application's session context. Tests verify source-statement preservation and idempotent repair. The running local database was backed up and repaired; production remains untouched.

Nine added test cases cover same-day ordering around disposals, future-date rejection, stamp refunds, stale diagnostics, closed-holding totals, independent snapshot valuation coverage, cash-flow consistency, and stored-lot repair. The frontend build and lint passed again, and the live local dashboard was checked after repair.

For hosted deployments, set `APP_SECRET` and `DATABASE_URL`, and keep the API and worker on the same environment. App entry does not require a password. See the README for local startup and upgrade instructions. Re-upload existing statements once to rebuild data under calculation version 3.0.1. Back up the application database before migration.

Production deployment, a Docker image build, real-provider latency under load, and private encrypted-PDF end-to-end cases were not verified in this run. Each installation still has one shared portfolio database. Unavailable benchmark/market-cap datasets and missing donor/acquisition histories are reported explicitly. Tax-rule changes were not part of this implementation. Gains responses and rendering are paginated; the selected scope's full eligible disposal set is still read to calculate year totals, so extremely large gain histories remain a candidate for separate precomputed aggregates.

## Replacement-flow follow-up

Before the new dataset commits, upload progress no longer invalidates portfolio queries. Queued-to-processing transitions retain one poller. The view switches once matching statement metadata is available, and delayed focus/sync responses cannot restore an older dataset. Failed/cancelled imports restore the saved statement. Local and hosted installations open without an app password. Legacy owner-password settings are ignored.

Seven frontend regression tests pass, covering replacement publication, no old-dataset refetch, stale metadata and focus responses, reload recovery, failure/cancellation, and direct app entry without a session request. Frontend lint/build pass and npm audit reports zero known vulnerabilities for the current lockfile.
