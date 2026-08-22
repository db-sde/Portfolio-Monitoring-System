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
const NO_FILTER_BAR = new Set(['portfolio-summary', 'settings'])

export default function App() {
  const [page, setPage] = useState('dashboard')
  const [config, setConfig] = useState(null)
  const [uploadInfo, setUploadInfo] = useState(null)
  const [checkingInitial, setCheckingInitial] = useState(true)
  const [enrichStatus, setEnrichStatus] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState(null)
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

  const loadConfig = useCallback(() => {
    api.getConfig().then(setConfig).catch(() => {})
  }, [])

  const pollEnrichStatus = useCallback(() => {
    let cancelled = false
    const tick = () => {
      api.getEnrichStatus().then((status) => {
        if (cancelled) return
        setEnrichStatus(status)
        if (status.pending > 0) setTimeout(tick, 2000)
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
    try {
      const result = await api.uploadCas(file, password)
      setUploadInfo({ investor_name: result.investor_name, statement_period: result.statement_period })
      setRefreshTick((t) => t + 1)
      pollEnrichStatus()
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

  const PageComponent = PAGES[page]

  return (
    <div className="flex min-h-screen">
      <Sidebar active={page} open={sidebarOpen} onClose={() => setSidebarOpen(false)} onNavigate={(k) => { setPage(k); setSidebarOpen(false) }} />
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
