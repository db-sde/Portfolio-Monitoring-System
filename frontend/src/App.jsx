import { useEffect, useState, useCallback } from 'react'
import { api } from './api'
import Sidebar from './components/Sidebar'
import TopBar from './components/TopBar'
import LevelSelector from './components/LevelSelector'
import WelcomeUpload from './components/WelcomeUpload'
import Dashboard from './pages/Dashboard'
import Portfolio from './pages/Portfolio'
import Transactions from './pages/Transactions'
import CapitalGains from './pages/CapitalGains'
import PortfolioSnapshot from './pages/PortfolioSnapshot'
import FundSummary from './pages/FundSummary'
import PortfolioSummary from './pages/PortfolioSummary'
import Exposure from './pages/Exposure'
import Settings from './pages/Settings'

const PAGES = {
  dashboard: Dashboard,
  portfolio: Portfolio,
  transactions: Transactions,
  'capital-gains': CapitalGains,
  snapshot: PortfolioSnapshot,
  'fund-summary': FundSummary,
  'portfolio-summary': PortfolioSummary,
  exposure: Exposure,
  settings: Settings,
}

// Pages that don't use the level/advisor filter bar at all (summary is
// already grouped by every advisor at once; settings isn't portfolio data).
// "upload" never reaches this set — it's handled before the shell renders.
const NO_FILTER_BAR = new Set(['portfolio-summary', 'settings'])

// Sidebar navigation used to be plain useState with no URL involved at
// all — every page lived at the same "/", so the browser's Back button
// had nothing of this app's own to go back to (it would just leave the
// app entirely), and reloading always landed back on Dashboard no
// matter which page you'd been on. pageFromPath/pathFromPage are the
// two directions of keeping `page` state in sync with the real URL via
// the History API, so Back/Forward/reload all do what they look like
// they should.
// "/" is its own page — the upload/drop screen — not an alias for
// Dashboard; "upload" is a pseudo-key PAGES doesn't have an entry for,
// handled specially in the render below (it always shows WelcomeUpload,
// even once data exists — a deliberate, bookmarkable "start fresh" entry
// point now that a new upload replaces what's there instead of adding
// to it). "/dashboard" is unaffected and works exactly as before.
function pageFromPath(pathname) {
  const key = pathname.replace(/^\/+/, '')
  if (key === '') return 'upload'
  return PAGES[key] ? key : 'dashboard'
}
function pathFromPage(page) {
  return page === 'upload' ? '/' : `/${page}`
}

// "Has a statement been parsed in THIS browser session?" — the flag that
// separates "reloaded the page mid-use" (keep showing it) from "came
// back later" (offer to parse again). sessionStorage, not localStorage,
// precisely because it dies with the tab.
//
// Every access is guarded: sessionStorage doesn't merely return null
// when unavailable, it THROWS on access in a browser set to block site
// data and in some private-browsing modes. An unguarded read here would
// take the whole app down on load for those users. Failing closed (as
// if nothing was parsed) is the right fallback — worst case someone is
// asked to parse again, which is this flow's normal behaviour anyway.
const PARSED_THIS_SESSION_KEY = 'portfolioiq.parsedThisSession'

function hasParsedThisSession() {
  try {
    return window.sessionStorage.getItem(PARSED_THIS_SESSION_KEY) === '1'
  } catch {
    return false
  }
}

function markParsedThisSession() {
  try {
    window.sessionStorage.setItem(PARSED_THIS_SESSION_KEY, '1')
  } catch {
    // Non-fatal: the current render already shows the parsed statement.
    // Only a later reload would fall back to the parse screen.
  }
}

function clearParsedThisSession() {
  try {
    window.sessionStorage.removeItem(PARSED_THIS_SESSION_KEY)
  } catch {
    /* nothing to clear if storage is unavailable */
  }
}

export default function App() {
  const [page, setPage] = useState(() => pageFromPath(window.location.pathname))
  const [config, setConfig] = useState(null)
  const [uploadInfo, setUploadInfo] = useState(null)
  const [checkingInitial, setCheckingInitial] = useState(true)
  const [enrichStatus, setEnrichStatus] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState(null)
  const [uploadNotice, setUploadNotice] = useState(null)
  const [initialLoadError, setInitialLoadError] = useState(null)
  const [refreshTick, setRefreshTick] = useState(0)
  const [sidebarOpen, setSidebarOpen] = useState(false)

  const [filters, setFilters] = useState({
    includeZeroValue: false,
    level: null,
    groupName: null,
    investorName: null,
    arn: null,
  })

  const navigate = useCallback((nextPage) => {
    if (nextPage === page) return
    window.history.pushState({ page: nextPage }, '', pathFromPage(nextPage))
    setPage(nextPage)
  }, [page])

  useEffect(() => {
    // Establish a proper history entry for the very first page too (so
    // the initial load isn't a state-less entry Back can't distinguish
    // from "leave the app"), then follow the browser's own Back/Forward.
    window.history.replaceState({ page }, '', pathFromPage(page))
    const onPopState = (event) => {
      setPage(event.state?.page || pageFromPath(window.location.pathname))
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const loadConfig = useCallback(() => {
    api.getConfig().then(setConfig).catch(() => {})
  }, [])

  const pollEnrichStatus = useCallback(() => {
    let cancelled = false
    // 180 tries * 2s = 6 minutes — generous (the largest real portfolio
    // enriched this session took ~6 minutes), but not unbounded: if the
    // background task itself dies outright (an exception before it
    // writes even one EnrichmentCache row), pending would otherwise
    // never reach 0 and this would poll forever instead of just giving
    // up and leaving the last-known status on screen.
    const MAX_ATTEMPTS = 180
    let attempts = 0
    const tick = () => {
      api.getEnrichStatus().then((status) => {
        if (cancelled) return
        setEnrichStatus(status)
        attempts += 1
        if (status.pending > 0 && attempts < MAX_ATTEMPTS) setTimeout(tick, 2000)
        else setRefreshTick((t) => t + 1)
      }).catch(() => {})
    }
    tick()
    return () => { cancelled = true }
  }, [])

  const [retrying, setRetrying] = useState(false)
  const handleRetryEnrichment = useCallback(() => {
    setRetrying(true)
    api.retryEnrichment()
      .then((res) => {
        if (res.count > 0) pollEnrichStatus()
      })
      .catch(() => {})
      .finally(() => setRetrying(false))
  }, [pollEnrichStatus])

  useEffect(() => {
    loadConfig()

    // This app parses a statement per visit; it is not somewhere your
    // portfolio lives between visits. The database still holds the last
    // parse (it has to — every page reads from it while you're using
    // the app), but arriving fresh must never silently restore it.
    // Previously this fetched the portfolio on mount and, if any data
    // existed, dropped you straight into someone's dashboard without
    // your having uploaded anything this visit.
    //
    // hasParsedThisSession() is the whole distinction, and sessionStorage
    // is what makes it exactly right: it survives a refresh and in-app
    // navigation (so reloading mid-use keeps your statement on screen —
    // that's a normal thing to do while enrichment is still filling in),
    // and it is dropped when the tab closes, which is precisely "the
    // user left". A returning visitor lands on the parse screen and
    // chooses to parse again.
    if (!hasParsedThisSession()) {
      // Cheap liveness check in place of the portfolio fetch: a broken
      // backend must still be visible on load. A real incident (the
      // server rejecting everything with 500 "API_KEY is not set")
      // looked exactly like an empty account, with nothing on screen
      // saying otherwise — so this failure is surfaced through the same
      // banner WelcomeUpload renders for a failed upload, rather than
      // being discovered only by trying to upload. Using /api/config
      // instead of /api/portfolio also drops a multi-second query from
      // the load path of a visitor who is about to upload anyway.
      api.getConfig()
        .catch((err) => setInitialLoadError(err.message || 'Could not reach the backend.'))
        .finally(() => setCheckingInitial(false))
      return
    }

    api.getEnrichStatus().then(setEnrichStatus).catch(() => {})
    api.getPortfolio({ include_zero_value: true }).then((p) => {
      // holdings_coverage_through is null until a CAS has actually been
      // imported — the difference between "nothing uploaded" and
      // "uploaded, but every holding filtered out". If this session
      // thinks it parsed but the data is gone (wiped from Settings, or
      // replaced by an upload in another tab), drop the marker so the
      // next load correctly offers to parse instead of showing nothing.
      if (!p.holdings_coverage_through) {
        clearParsedThisSession()
        return
      }
      setUploadInfo({
        investor_name: (p.investor_names || []).join(', ') || undefined,
        statement_period: { from: p.holdings_coverage_from, to: p.holdings_coverage_through },
      })
    }).catch((err) => {
      setInitialLoadError(err.message || 'Could not reach the backend.')
    }).finally(() => setCheckingInitial(false))
  }, [loadConfig])

  // The wipe+ingest itself runs in the background now (a real ~50-scheme
  // statement is minutes of sequential mfapi.in calls — long enough that
  // Render's own reverse proxy used to return a 502 before the ingest
  // even finished, regardless of whether it was correct). uploadCas
  // returns almost immediately with a job_id; this polls the same way
  // pollEnrichStatus already does, but as a Promise handleUpload can
  // await, since — unlike enrichment — the rest of the upload flow
  // (landing on Dashboard, showing the imported statement) can't start
  // until ingest has actually finished.
  const pollUploadStatus = (jobId) => new Promise((resolve, reject) => {
    // 200 tries * 3s = 10 minutes — generous for a large real statement's
    // worth of sequential third-party API calls, but not unbounded.
    const MAX_ATTEMPTS = 200
    let attempts = 0
    const tick = () => {
      api.getUploadStatus(jobId).then((status) => {
        if (status.status === 'processing') {
          attempts += 1
          if (attempts >= MAX_ATTEMPTS) {
            reject(new Error('This is taking much longer than usual. It may still finish in the background — check back in a few minutes.'))
            return
          }
          setTimeout(tick, 3000)
        } else if (status.status === 'error') {
          reject(new Error(status.message || 'Importing this statement failed.'))
        } else {
          resolve(status)
        }
      }).catch(reject)
    }
    tick()
  })

  const handleUpload = async (file, password = '') => {
    setUploading(true)
    setUploadError(null)
    setUploadNotice(null)
    try {
      const submitted = await api.uploadCas(file, password)
      if (submitted.status === 'duplicate') {
        // Not a failure, and no longer a dead end. Since a fresh visit
        // always starts at the parse screen, the ordinary way to come
        // back to your own statement is to submit it again — which
        // lands here. The parsed data for this exact file is already in
        // the database and correct, so the honest outcome is to show
        // it: same as a successful parse, minus the wait. Previously
        // this returned without setting uploadInfo, which (once page
        // load stopped restoring state) left the user stuck on the
        // upload screen being told their statement was already loaded
        // while none of it was on screen.
        setUploadNotice('Already parsed — showing your statement.')
        markParsedThisSession()
        setUploadInfo({
          investor_name: submitted.investor_name,
          statement_period: submitted.statement_period,
        })
        setRefreshTick((t) => t + 1)
        pollEnrichStatus()
        if (page === 'upload') navigate('dashboard')
        return
      }
      const result = await pollUploadStatus(submitted.job_id)
      markParsedThisSession()
      setUploadInfo({ investor_name: result.investor_name, statement_period: result.statement_period })
      setRefreshTick((t) => t + 1)
      pollEnrichStatus()
      // Uploading from the dedicated "/" drop screen is a "give me
      // results" action — land on Dashboard rather than leaving them on
      // the upload screen once there's something to actually show.
      if (page === 'upload') navigate('dashboard')
    } catch (err) {
      setUploadError(err.message)
    } finally {
      setUploading(false)
    }
  }

  if (checkingInitial) {
    return <div className="min-h-screen" />
  }

  if (!uploadInfo) {
    // uploadError (a failed upload attempt) takes priority once the user
    // has actually tried something; initialLoadError (the page's own
    // first load failing) is the fallback so a misconfigured/unreachable
    // backend is visible even before they've touched anything.
    return <WelcomeUpload onUpload={handleUpload} uploading={uploading} error={uploadError || initialLoadError} />
  }

  // "/" is always the bare drop-box screen, full-screen and standalone —
  // no sidebar, no topbar, no investor name/enrich badge — even once
  // data exists. It used to sit inside the normal shell in that case
  // (so the topbar's own already-loaded investor name stayed visible
  // while replacing), but that read as a stray "preloaded statement"
  // showing up unexplained the moment the app opened — reported live.
  // "upload" isn't in PAGES for this reason: it's handled here, before
  // the shell below, rather than as a PageComponent inside it.
  if (page === 'upload') {
    return <WelcomeUpload onUpload={handleUpload} uploading={uploading} error={uploadError} replacing />
  }

  const PageComponent = PAGES[page]

  return (
    <div className="flex min-h-screen">
      <Sidebar active={page} open={sidebarOpen} onClose={() => setSidebarOpen(false)} onNavigate={(k) => { navigate(k); setSidebarOpen(false) }} />
      <div className="flex-1 flex flex-col min-w-0">
        <TopBar
          investorName={uploadInfo?.investor_name}
          statementPeriod={uploadInfo?.statement_period}
          lastEnriched={enrichStatus?.last_run}
          enrichStatus={enrichStatus}
          onUpload={handleUpload}
          uploading={uploading}
          onMenuClick={() => setSidebarOpen(true)}
          onRetryEnrichment={handleRetryEnrichment}
          retryingEnrichment={retrying}
        />
        {uploadError && (
          <div className="mx-4 md:mx-6 mt-4 rounded-lg border border-bad/20 bg-bad-tint text-bad text-sm px-4 py-2.5">
            {uploadError}
          </div>
        )}
        {uploadNotice && (
          <div className="mx-4 md:mx-6 mt-4 rounded-lg border border-accent/20 bg-accent-tint text-accent-strong text-sm px-4 py-2.5">
            {uploadNotice}
          </div>
        )}
        <main className="flex-1 p-4 md:p-6 max-w-[1400px] w-full">
          {!NO_FILTER_BAR.has(page) && (
            <div className="mb-5">
              <LevelSelector
                config={config}
                level={filters.level}
                groupName={filters.groupName}
                investorName={filters.investorName}
                arn={filters.arn}
                onChange={(next) => setFilters((f) => ({ ...f, ...next }))}
              />
            </div>
          )}
          <PageComponent filters={filters} setFilters={setFilters} config={config} refreshTick={refreshTick} onConfigSaved={loadConfig} />
        </main>
      </div>
    </div>
  )
}
