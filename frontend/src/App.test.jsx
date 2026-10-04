import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { api, invalidateQueries } from './api'

vi.mock('./api', () => ({
  invalidateQueries: vi.fn(),
  api: Object.fromEntries(['getConfig', 'getStatement', 'getEnrichStatus', 'getCurrentJob',
    'getUploadStatus', 'uploadCas', 'getPortfolio', 'cancelJob', 'logout'].map(k => [k, vi.fn()])),
}))
vi.mock('recharts', () => {
  const Chart = ({ children }) => <div>{children}</div>
  return Object.fromEntries(['PieChart', 'Pie', 'Cell', 'ResponsiveContainer', 'Tooltip',
    'BarChart', 'Bar', 'XAxis', 'YAxis', 'CartesianGrid'].map(k => [k, Chart]))
})

const statement = id => ({ dataset_id: id, investor_name: id === 1 ? 'Old Investor' : 'New Investor', statement_period: { from: '2023-01-01', to: '2024-01-01' } })
const portfolio = id => ({
  dataset_id: id, data_quality: 'OK', exposure: {},
  schemes: [{ holding_id: id, scheme_name: id === 1 ? 'Old Fund' : 'New Fund', balance_units: 1, flags: [] }],
  subtotals: { total: { current_value: id * 100, total_holdings: 1 } },
})
const queued = { job_id: 7, kind: 'upload', status: 'queued', stage: 'queued', ready: false }
const processing = stage => ({ ...queued, status: 'processing', stage })
const ready = { ...queued, status: 'processing', stage: 'enriching', ready: true, dataset_id: 2 }
function deferred() {
  let resolve
  const promise = new Promise(r => { resolve = r })
  return { promise, resolve }
}
async function start() {
  render(<App />)
  await screen.findByText('Old Fund')
  vi.useFakeTimers()
}
async function upload() {
  const input = document.querySelector('input[type="file"]')
  await act(async () => { fireEvent.change(input, { target: { files: [new File(['{}'], 'new.json', { type: 'application/json' })] } }) })
}
async function poll() {
  await act(async () => { await vi.advanceTimersByTimeAsync(2500) })
}
function expectImportOnly() {
  expect(screen.getByText('Importing your new statement')).toBeTruthy()
  expect(screen.queryByText('Old Fund')).toBeNull()
  expect(screen.queryByText('Old Investor')).toBeNull()
}

beforeEach(() => {
  vi.clearAllMocks()
  history.replaceState({}, '', '/dashboard')
  api.getConfig.mockResolvedValue({ groups: [], preferences: {} })
  api.getStatement.mockResolvedValue(statement(1))
  api.getCurrentJob.mockResolvedValue(null)
  api.getEnrichStatus.mockResolvedValue({ total_schemes: 1, pending: 0, failed: 0 })
  api.getPortfolio.mockResolvedValue(portfolio(1))
  api.uploadCas.mockResolvedValue(queued)
  api.getUploadStatus.mockResolvedValue(processing('parsing'))
})
afterEach(() => { cleanup(); vi.useRealTimers() })

describe('statement replacement', () => {
  it('keeps the old dataset hidden without refetching it and publishes the new dataset once', async () => {
    await start()
    await upload()
    expectImportOnly()
    expect(api.getUploadStatus).toHaveBeenCalledTimes(1) // queued -> processing must not restart the effect
    api.getUploadStatus.mockResolvedValue(processing('resolving'))
    await poll()
    expectImportOnly()
    expect(api.getPortfolio).toHaveBeenCalledTimes(1)
    expect(api.getEnrichStatus).toHaveBeenCalledTimes(1)
    expect(invalidateQueries).not.toHaveBeenCalled()
    api.getStatement.mockResolvedValue(statement(2))
    api.getPortfolio.mockResolvedValue(portfolio(2))
    api.getUploadStatus.mockResolvedValue(ready)
    await poll()
    expect(screen.getByText('New Fund')).toBeTruthy()
    expect(screen.getByText('New Investor')).toBeTruthy()
    expect(screen.queryByText('Old Fund')).toBeNull()
    expect(api.getPortfolio).toHaveBeenCalledTimes(2)
    expect(invalidateQueries).toHaveBeenCalledTimes(1)
    await poll() // unchanged enrichment progress must not reload the page
    expect(api.getPortfolio).toHaveBeenCalledTimes(2)
  })

  it('does not let an old focus request overwrite the newly published statement', async () => {
    await start()
    const oldSync = deferred()
    api.getStatement.mockReturnValueOnce(oldSync.promise)
    await act(async () => { fireEvent.focus(window) })
    await upload()
    api.getStatement.mockResolvedValue(statement(2))
    api.getPortfolio.mockResolvedValue(portfolio(2))
    api.getUploadStatus.mockResolvedValue(ready)
    await poll()
    await act(async () => { oldSync.resolve(statement(1)) })
    expect(screen.getByText('New Investor')).toBeTruthy()
    expect(screen.getByText('New Fund')).toBeTruthy()
    expect(api.getPortfolio).toHaveBeenCalledTimes(2)
  })

  it('waits for matching statement metadata instead of reopening the old dataset', async () => {
    await start()
    await upload()
    api.getUploadStatus.mockResolvedValue(ready)
    await poll() // metadata still reports dataset 1
    expectImportOnly()
    expect(api.getPortfolio).toHaveBeenCalledTimes(1)
    api.getStatement.mockResolvedValue(statement(2))
    api.getPortfolio.mockResolvedValue(portfolio(2))
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(screen.getByText('New Fund')).toBeTruthy()
    expect(api.getPortfolio).toHaveBeenCalledTimes(2)
  })

  it.each(['error', 'cancelled'])('restores the saved portfolio after an import is %s', async status => {
    await start()
    await upload()
    api.getUploadStatus.mockResolvedValue({ ...processing(status), status, message: 'Could not parse statement' })
    await poll()
    expect(screen.getByText('Old Fund')).toBeTruthy()
    expect(screen.queryByText('Importing your new statement')).toBeNull()
    expect(invalidateQueries).not.toHaveBeenCalled()
  })

  it('resumes an in-progress import after reload without mounting the old portfolio', async () => {
    api.getCurrentJob.mockResolvedValue(processing('parsing'))
    render(<App />)
    await screen.findByText('Importing your new statement')
    await waitFor(() => expect(api.getUploadStatus).toHaveBeenCalledTimes(1))
    expect(api.getPortfolio).not.toHaveBeenCalled()
    expect(screen.queryByText('Old Fund')).toBeNull()
  })
})
