const BASE = import.meta.env.VITE_API_BASE_URL || ''
const cache = new Map()
let generation = 0
export function invalidateQueries() { generation += 1; cache.clear() }

export async function request(path, options = {}) {
  const method = options.method || 'GET'
  const cacheable = method === 'GET' && !/session|status|jobs|statement|config/.test(path)
  const key = `${generation}:${path}`
  const previous = cache.get(key)
  if (cacheable && previous && previous.expires > Date.now()) return previous.promise
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), options.body instanceof FormData ? 120000 : 45000)
  const fetchResult = async () => {
    try {
      const res = await fetch(`${BASE}${path}`, {
        ...options, credentials: 'include', signal: controller.signal,
        headers: { ...options.headers, 'X-Requested-With': 'PortfolioIQ' },
      })
      if (!res.ok) {
        let message = `Request failed (${res.status})`
        try { const body = await res.json(); message = body.detail || message } catch { /* Non-JSON proxy error. */ }
        if (res.status === 401 && path !== '/api/login') window.dispatchEvent(new Event('session-expired'))
        throw new Error(typeof message === 'string' ? message : JSON.stringify(message))
      }
      if (method !== 'GET') invalidateQueries()
      return options.blob ? res.blob() : res.json()
    } catch (err) {
      cache.delete(key)
      if (err.name === 'AbortError') throw new Error('The server took too long to respond. Please retry; an import may still be running.')
      throw err
    } finally { clearTimeout(timeout) }
  }
  const promise = fetchResult()
  if (cache.size >= 100) cache.delete(cache.keys().next().value)
  if (cacheable) cache.set(key, { promise, expires: Date.now() + 15000 })
  return promise
}
function qs(params = {}) {
  const values = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) if (value !== undefined && value !== null) values.set(key, value)
  return values.size ? `?${values}` : ''
}
const get = (path) => (params) => request(path + qs(params))
export const api = {
  getSession: () => request('/api/session'),
  login: (password) => request('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password }) }),
  logout: () => request('/api/logout', { method: 'POST' }),
  getStatement: () => request('/api/statement'),
  getCurrentJob: () => request('/api/jobs/current'),
  cancelJob: (id) => request(`/api/jobs/${id}/cancel`, { method: 'POST' }),
  uploadCas(file, password = '') {
    const body = new FormData(); body.append('file', file); body.append('password', password)
    return request('/api/upload-cas', { method: 'POST', body })
  },
  getUploadStatus: (id) => request(`/api/upload-status/${id}`),
  getPortfolio: get('/api/portfolio'),
  getSnapshot: get('/api/portfolio/snapshot'),
  getPortfolioSummary: get('/api/portfolio/summary'),
  getFundSummary: get('/api/portfolio/fund-summary'),
  getExposure: get('/api/portfolio/exposure'),
  getTransactions: get('/api/transactions'),
  getCapitalGains: get('/api/capital-gains'),
  download112aCsv: (params) => request('/api/capital-gains/112a.csv' + qs(params), { blob: true }),
  getDataQuality: get('/api/data-quality'),
  getConfig: () => request('/api/config'),
  saveConfig: (config) => request('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(config) }),
  getEnrichStatus: () => request('/api/enrich/status'),
  retryEnrichment: () => request('/api/enrich/retry', { method: 'POST' }),
  deleteAllData: () => request('/api/all-data', { method: 'DELETE' }),
}
