// Relative by default: in production this is same-origin (Vercel's
// vercel.json rewrites /api/* to the Render backend), and in local dev
// the Vite dev server proxies /api itself (see vite.config.js) — so the
// backend's actual URL never needs to be baked into the built bundle.
// VITE_API_BASE_URL is still available as an override (e.g. pointing a
// local frontend at an ngrok tunnel instead of the dev proxy).
const BASE = import.meta.env.VITE_API_BASE_URL || ''
// The backend rejects every request without this (see main.py's
// _require_api_key) — a real production incident (all data wiped by an
// unauthenticated caller, most likely found via the public GitHub repo)
// is why this exists at all. Not a substitute for real per-user auth —
// this value ships in the built bundle, so it's visible to anyone who
// inspects this app's own network requests — but it stops a stranger or
// bot from calling the API directly without ever loading the app.
const API_KEY = import.meta.env.VITE_API_KEY || ''

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    ...options,
    headers: { ...options.headers, 'X-API-Key': API_KEY },
  })
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
  uploadCas(file, password = '') {
    const form = new FormData()
    form.append('file', file)
    form.append('password', password)
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
  getExposure(params) {
    return request(`/api/portfolio/exposure${qs(params)}`)
  },
  getTransactions(params) {
    return request(`/api/transactions${qs(params)}`)
  },
  getCapitalGains(params) {
    return request(`/api/capital-gains${qs(params)}`)
  },
  // The one endpoint that isn't JSON (spec 12.2: the parser's own
  // verified Schedule 112A export) — same BASE-prefixing as request()
  // so the VITE_API_BASE_URL override still works for it, just a Blob
  // instead of a parsed body.
  async download112aCsv(params) {
    const res = await fetch(`${BASE}/api/capital-gains/112a.csv${qs(params)}`, {
      headers: { 'X-API-Key': API_KEY },
    })
    if (!res.ok) throw new Error(`Export failed (${res.status})`)
    return res.blob()
  },
  getDataQuality() {
    return request('/api/data-quality')
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
  // Full reset (spec 19) — wipes everything a plain new upload leaves
  // alone too: config, groups/investors/ARNs, preferences. Had no UI
  // path at all until now (backend-only, curl/API-client only), found
  // auditing the app end to end — a real "wipe everything" need had
  // come up with no way to do it except a raw API call.
  deleteAllData() {
    return request('/api/all-data', { method: 'DELETE' })
  },
}
