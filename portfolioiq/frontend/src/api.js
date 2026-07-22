const BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, options)
  if (!res.ok) {
    let message = `Request failed (${res.status})`
    try {
      const body = await res.json()
      message = body.detail || message
    } catch {
      // response body wasn't JSON; keep the generic message
    }
    throw new Error(typeof message === 'string' ? message : JSON.stringify(message))
  }
  return res.json()
}

function qs(params = {}) {
  const usp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') usp.set(k, v)
  }
  const s = usp.toString()
  return s ? `?${s}` : ''
}

export const api = {
  uploadCas(file) {
    const form = new FormData()
    form.append('file', file)
    return request('/api/upload-cas', { method: 'POST', body: form })
  },
  getPortfolio(params) {
    return request(`/api/portfolio${qs(params)}`)
  },
  getSnapshot(params) {
    return request(`/api/portfolio/snapshot${qs(params)}`)
  },
  getPortfolioSummary() {
    return request('/api/portfolio/summary')
  },
  getFundSummary(params) {
    return request(`/api/portfolio/fund-summary${qs(params)}`)
  },
  getExposure() {
    return request('/api/portfolio/exposure')
  },
  getConfig() {
    return request('/api/config')
  },
  saveConfig(config) {
    return request('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    })
  },
  getEnrichStatus() {
    return request('/api/enrich/status')
  },
}
