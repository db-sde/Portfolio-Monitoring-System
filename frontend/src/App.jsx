import { useEffect, useState, useCallback, useRef, lazy, Suspense } from 'react'
import { api, invalidateQueries } from './api'
import Sidebar from './components/Sidebar'
import TopBar from './components/TopBar'
import LevelSelector from './components/LevelSelector'
import WelcomeUpload from './components/WelcomeUpload'
const Dashboard = lazy(() => import('./pages/Dashboard'))
const Portfolio = lazy(() => import('./pages/Portfolio'))
const Transactions = lazy(() => import('./pages/Transactions'))
const CapitalGains = lazy(() => import('./pages/CapitalGains'))
const PortfolioSnapshot = lazy(() => import('./pages/PortfolioSnapshot'))
const FundSummary = lazy(() => import('./pages/FundSummary'))
const PortfolioSummary = lazy(() => import('./pages/PortfolioSummary'))
const Exposure = lazy(() => import('./pages/Exposure'))
const Settings = lazy(() => import('./pages/Settings'))

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

const NO_FILTER_BAR = new Set(['portfolio-summary', 'settings'])
const pageFromPath = () => { const key = location.pathname.replace(/^\/+/, ''); return key === '' ? 'upload' : PAGES[key] ? key : 'dashboard' }
const active = (job) => job && ['queued', 'processing'].includes(job.status)

export default function App() {
  const [page, setPage] = useState(pageFromPath)
  const [config, setConfig] = useState(null)
  const [uploadInfo, setUploadInfo] = useState(null)
  const [checking, setChecking] = useState(true)
  const [enrichStatus, setEnrichStatus] = useState(null)
  const [job, setJob] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [replacing, setReplacing] = useState(false)
  const [error, setError] = useState(null)
  const [pollError, setPollError] = useState(null)
  const [refreshTick, setRefreshTick] = useState(0)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [filters, setFilters] = useState({ includeZeroValue: false, level: null, groupName: null, investorName: null, arn: null })
  const dataset = useRef(null)
  const datasetRevision = useRef(0)
  const replacementPending = useRef(false)
  const currentJob = useRef(null)
  const updateJob = useCallback(value => { currentJob.current = value; setJob(value) }, [])
  const showReplacement = useCallback(value => { replacementPending.current = value; setReplacing(value) }, [])
  const dirty = useRef(false)
  const route = useRef(location.pathname)
  const navigate = useCallback((next) => {
    if (dirty.current && !window.confirm('Discard unsaved settings?')) return
    route.current = next === 'upload' ? '/' : `/${next}`
    history.pushState({}, '', route.current)
    setPage(next)
  }, [])
  useEffect(() => {
    const pop = () => {
      if (dirty.current && !window.confirm('Discard unsaved settings?')) { history.pushState({}, '', route.current); return }
      route.current = location.pathname; setPage(pageFromPath())
    }
    const changed = (e) => { dirty.current = e.detail }
    window.addEventListener('popstate', pop)
    window.addEventListener('settings-dirty', changed)
    return () => { window.removeEventListener('popstate', pop); window.removeEventListener('settings-dirty', changed) }
  }, [])
  const refresh = useCallback(() => { invalidateQueries(); setRefreshTick(t => t + 1) }, [])
  const publishStatement = useCallback(statement => {
    datasetRevision.current += 1
    dataset.current = statement?.dataset_id ?? null
    setUploadInfo(statement?.dataset_id ? statement : null)
    refresh()
  }, [refresh])
  const loadConfig = useCallback(async () => {
    try {
      const value = await api.getConfig(); setConfig(value)
      setFilters(f => ({ ...f, includeZeroValue: !!value.preferences?.show_zero_value_funds }))
      refresh()
    } catch (e) { setError(e.message) }
  }, [refresh])
  useEffect(() => {
    let live = true
    Promise.all([api.getConfig(), api.getStatement(), api.getEnrichStatus(), api.getCurrentJob()])
      .then(([c, statement, status, current]) => {
        if (!live) return
        setConfig(c); setFilters(f => ({ ...f, includeZeroValue: !!c.preferences?.show_zero_value_funds }))
        dataset.current = statement.dataset_id
        setUploadInfo(statement.dataset_id ? statement : null); setEnrichStatus(status); updateJob(current || status.last_job)
        showReplacement(active(current) && current.kind === 'upload' && (!current.ready || current.dataset_id !== statement.dataset_id))
        if (!current && status.last_job?.status === 'error') setError(status.last_job.message)
      }).catch(e => { if (live) setError(e.message) }).finally(() => { if (live) setChecking(false) })
    return () => { live = false }
  }, [showReplacement, updateJob])
  useEffect(() => {
    let live = true
    const sync = async () => {
      if (document.visibilityState !== 'visible' || replacementPending.current || active(currentJob.current)) return
      const revision = datasetRevision.current
      try {
        const [statement, current] = await Promise.all([api.getStatement(), api.getCurrentJob()])
        if (!live || replacementPending.current || revision !== datasetRevision.current || active(currentJob.current)) return
        if (active(current) && current.kind === 'upload') {
          // The poller owns publication while another tab is importing.
          showReplacement(!current.ready || current.dataset_id !== dataset.current)
          updateJob(current)
          return
        }
        if (statement.dataset_id !== dataset.current) {
          publishStatement(statement)
        }
        if (current) updateJob(current)
      } catch (e) { if (live) setPollError(`Could not check for portfolio updates: ${e.message}`) }
    }
    const timer = setInterval(sync, 30000)
    window.addEventListener('focus', sync)
    return () => { live = false; clearInterval(timer); window.removeEventListener('focus', sync) }
  }, [publishStatement, showReplacement, updateJob])
  // One poller owns recovery, progressive refresh, and completion. It survives reload
  // by discovering the durable job on mount, and cleans up on sign-out/unmount.
  useEffect(() => {
    if (!active(job)) return
    let live = true, timer, failures = 0, lastStage = job.stage
    const tick = async () => {
      try {
        const next = await api.getUploadStatus(job.job_id)
        if (!live) return
        failures = 0; setPollError(null)
        let published = false
        if (next.ready && dataset.current !== next.dataset_id) {
          const [statement, status] = await Promise.all([api.getStatement(), api.getEnrichStatus()])
          if (!live) return
          if (statement.dataset_id !== next.dataset_id) throw new Error('Waiting for the new statement to become available.')
          publishStatement(statement); setEnrichStatus(status); published = true
          showReplacement(false)
          navigate('dashboard')
        }
        if (next.ready && next.kind === 'upload') {
          showReplacement(false)
          if (pageFromPath() === 'upload') navigate('dashboard')
        }
        if (next.ready && !published && (next.stage !== lastStage || !active(next))) {
          const status = await api.getEnrichStatus()
          if (!live) return
          setEnrichStatus(status); refresh()
        }
        lastStage = next.stage
        updateJob(next)
        if (!active(next)) showReplacement(false)
        if (next.status === 'error') setError(next.message || 'Processing failed. Please retry.')
        if (active(next)) timer = setTimeout(tick, 2500)
      } catch (e) {
        if (!live) return
        setPollError(`Progress updates interrupted: ${e.message} Reconnecting…`)
        timer = setTimeout(tick, Math.min(30000, 2500 * 2 ** Math.min(++failures, 4)))
      }
    }
    tick()
    return () => { live = false; clearTimeout(timer) }
  }, [job?.job_id, navigate, refresh, publishStatement, showReplacement, updateJob])
  const handleUpload = async (file, password = '') => {
    if (replacementPending.current || active(currentJob.current)) return
    datasetRevision.current += 1
    showReplacement(true)
    setSubmitting(true); setError(null)
    try {
      const result = await api.uploadCas(file, password)
      if (result.status === 'duplicate') {
        const statement = await api.getStatement()
        publishStatement(statement); showReplacement(false); navigate('dashboard')
      } else updateJob(result)
    } catch (e) { showReplacement(false); setError(e.message) } finally { setSubmitting(false) }
  }
  const retry = async () => {
    setSubmitting(true); setError(null)
    try { updateJob(await api.retryEnrichment()) } catch (e) { setError(e.message) } finally { setSubmitting(false) }
  }
  const cancel = async () => {
    try { await api.cancelJob(job.job_id); updateJob({ ...currentJob.current, cancel_requested: true }) } catch (e) { setError(e.message) }
  }
  const busy = submitting || active(job)
  const status = <div className="px-4 py-2 text-sm" role="status" aria-live="polite">
    {active(job) && <span>Processing: {job.stage || job.status}. {job.ready && 'Your portfolio is ready while market data refreshes.'} <button className="underline ml-2" onClick={cancel} disabled={job.cancel_requested}>{job.cancel_requested ? 'Cancelling…' : 'Cancel'}</button></span>}
    {job?.status === 'cancelled' && <span>Processing cancelled.</span>}
    {(uploadInfo?.warnings || []).map((warning, i) => <p key={i} className="text-warn">{warning}</p>)}
    {(error || pollError) && <p className="text-bad">{error || pollError}</p>}
  </div>
  if (checking) return <div role="status" className="p-8">Loading your portfolio…</div>
  if (replacing) return <main className="min-h-screen flex flex-col items-center justify-center p-6 text-center" aria-busy="true">
    <h1 className="font-display text-2xl font-bold mb-3">Importing your new statement</h1>
    <p className="text-sm text-ink-2 mb-3">Your current statement stays saved until the new one is ready.</p>
    {submitting && <p role="status">Uploading statement…</p>}
    {status}
  </main>
  if (!uploadInfo || page === 'upload') return <>{status}<WelcomeUpload onUpload={handleUpload} uploading={busy} error={error} replacing={!!uploadInfo} />{uploadInfo && <button className="fixed top-4 right-4 underline" onClick={() => navigate('dashboard')}>Back to portfolio</button>}</>
  const PageComponent = PAGES[page]
  return <div className="flex min-h-screen">
    <Sidebar active={page} open={sidebarOpen} onClose={() => setSidebarOpen(false)} onNavigate={key => { navigate(key); setSidebarOpen(false) }} />
    <div className="flex-1 flex flex-col min-w-0">
      <TopBar investorName={uploadInfo.investor_name} statementPeriod={uploadInfo.statement_period} lastEnriched={enrichStatus?.last_run} enrichStatus={enrichStatus} onUpload={handleUpload} uploading={busy} onMenuClick={() => setSidebarOpen(true)} onRetryEnrichment={retry} retryingEnrichment={busy} />
      {status}
      <div className="px-4 flex gap-4 text-xs"><button onClick={retry} disabled={busy}>Refresh market data</button></div>
      <main className="flex-1 p-4 md:p-6 max-w-[1400px] w-full">
        {!NO_FILTER_BAR.has(page) && <div className="mb-5"><LevelSelector config={config} level={filters.level} groupName={filters.groupName} investorName={filters.investorName} arn={filters.arn} onChange={next => setFilters(f => ({ ...f, ...next }))} /></div>}
        <Suspense fallback={<div role="status">Loading page…</div>}><PageComponent key={`${uploadInfo.dataset_id}:${page}`} filters={filters} setFilters={setFilters} config={config} refreshTick={refreshTick} onConfigSaved={loadConfig} /></Suspense>
      </main>
    </div>
  </div>
}
