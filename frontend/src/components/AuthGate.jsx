import { cloneElement, useEffect, useState } from 'react'
import { api, invalidateQueries } from '../api'
export default function AuthGate({ children }) {
  const [authenticated, setAuthenticated] = useState(null)
  const [authRequired, setAuthRequired] = useState(true)
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    let live = true
    api.getSession().then((s) => { if (live) { setAuthenticated(s.authenticated); setAuthRequired(s.password_required !== false) } }).catch((e) => { if (live) { setError(e.message); setAuthenticated(false) } })
    const expired = () => { invalidateQueries(); setAuthenticated(false); setError('Your session expired. Sign in to continue.') }
    window.addEventListener('session-expired', expired)
    return () => { live = false; window.removeEventListener('session-expired', expired) }
  }, [])
  if (authenticated === null) return <div className="p-10 text-ink-2" role="status">Connecting to PortfolioIQ…</div>
  if (authenticated) return cloneElement(children, { authRequired })
  const login = async (event) => {
    event.preventDefault(); setBusy(true); setError(null)
    try { await api.login(password); setPassword(''); setAuthenticated(true) } catch (e) { setError(e.message) } finally { setBusy(false) }
  }
  return <main className="min-h-screen grid place-items-center p-6"><form onSubmit={login} className="w-full max-w-sm rounded-xl border border-line bg-card p-6 space-y-4">
    <h1 className="text-2xl font-bold">PortfolioIQ</h1><p className="text-ink-2">Sign in to your private portfolio workspace.</p>
    <label className="block">Owner password<input autoFocus required type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} className="block w-full border border-line rounded-lg p-2 mt-2" /></label>
    {error && <p role="alert" className="text-bad text-sm">{error}</p>}
    <button disabled={busy} className="rounded-lg bg-band text-band-ink px-4 py-2">{busy ? 'Signing in…' : 'Sign in'}</button>
  </form></main>
}
