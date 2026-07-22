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
    api.getPortfolio({}).then((p) => {
      setUploadInfo({ investor_name: p.investor_info?.name, statement_period: p.statement_period })
    }).catch(() => {}).finally(() => setCheckingInitial(false))
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
    return <WelcomeUpload onUpload={handleUpload} uploading={uploading} error={uploadError} />
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
