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
// already grouped by every advisor at once; settings isn't portfolio data;
// upload is the drop-box screen, not a portfolio view).
const NO_FILTER_BAR = new Set(['portfolio-summary', 'settings', 'upload'])

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

  useEffect(() => {
    loadConfig()
    api.getEnrichStatus().then(setEnrichStatus).catch(() => {})
    api.getPortfolio({ include_zero_value: true }).then((p) => {
      // Postgres just has empty tables before the first upload — unlike
      // the old cas_data.json-missing 404, this endpoint always returns
      // 200. holdings_coverage_through (null until a CAS has actually
      // been imported) is what distinguishes "nothing uploaded yet" from
      // "uploaded, but every holding got filtered out."
      if (!p.holdings_coverage_through) return
      setUploadInfo({
        investor_name: (p.investor_names || []).join(', ') || undefined,
        statement_period: { from: p.holdings_coverage_from, to: p.holdings_coverage_through },
      })
    }).catch((err) => {
      // A failed request here used to be swallowed identically to "no
      // data yet" — a real incident (the backend rejecting every
      // request with 500 "API_KEY is not set") looked exactly like an
      // empty account, with no indication anything was actually wrong.
      // Surfaced through the same error banner WelcomeUpload already
      // renders for a failed upload, so a misconfigured/unreachable
      // backend is visible on page load, not just discoverable by
      // trying to upload and having that fail too.
      setInitialLoadError(err.message || 'Could not reach the backend.')
    }).finally(() => setCheckingInitial(false))
  }, [loadConfig])

  const handleUpload = async (file, password = '') => {
    setUploading(true)
    setUploadError(null)
    setUploadNotice(null)
    try {
      const result = await api.uploadCas(file, password)
      if (result.status === 'duplicate') {
        // The backend's success shape (investor_name/statement_period) and
        // its duplicate shape (message/upload_id) are different — this used
        // to be read as if it were always the success shape, so a
        // duplicate's undefined investor_name/statement_period got set
        // silently and nothing on screen indicated the upload had actually
        // been rejected. A user re-uploading (or uploading a file that
        // happened to already be in the system) saw no error, no change,
        // and no explanation — exactly what "I uploaded a different file
        // and everything stayed the same" describes.
        //
        // This isn't a failure, though — the data already on screen *is*
        // this exact statement, correctly. A red error banner claiming
        // something went wrong (and implying nothing was ever imported)
        // is the wrong tone for "you're already looking at this"; a
        // neutral notice is what's actually true here.
        setUploadNotice("This statement is already loaded — you're looking at its current data.")
        return
      }
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

  // "upload" isn't in PAGES — it's the drop-box screen (WelcomeUpload),
  // rendered inline below rather than as a PageComponent so it can still
  // sit inside the normal sidebar/top-bar shell once data exists (only
  // the true no-data-yet case above renders it full-screen, standalone).
  const PageComponent = page === 'upload' ? null : PAGES[page]

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
          {page === 'upload' ? (
            <WelcomeUpload onUpload={handleUpload} uploading={uploading} error={uploadError} />
          ) : (
            <PageComponent filters={filters} setFilters={setFilters} config={config} refreshTick={refreshTick} onConfigSaved={loadConfig} />
          )}
        </main>
      </div>
    </div>
  )
}
