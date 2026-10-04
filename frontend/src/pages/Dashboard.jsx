import { useEffect, useState } from 'react'
import { api } from '../api'
import { formatIndian, formatPct, formatDate } from '../components/IndianNumber'
import SkeletonTable from '../components/SkeletonTable'
import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip, BarChart, Bar, XAxis, YAxis, CartesianGrid } from 'recharts'

const CAP_COLORS = ['#12172a', '#a9762f', '#c9b08a', '#738276']
const ASSET_LABELS = { EQUITY: 'Equity', HYBRID: 'Hybrid', DEBT: 'Debt', OTHER: 'Other' }

function StatCard({ label, value, tone = 'default' }) {
  const toneClass = tone === 'good' ? 'text-good' : tone === 'bad' ? 'text-bad' : 'text-ink'
  return (
    <div className="rounded-xl border border-line-soft bg-card p-4">
      <div className="text-xs font-medium text-ink-3 mb-1">{label}</div>
      <div className={`font-display text-2xl font-bold tabular ${toneClass}`}>{value}</div>
    </div>
  )
}

export default function Dashboard({ filters, refreshTick }) {
  const [portfolio, setPortfolio] = useState(null)
  const [exposure, setExposure] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [showClosed, setShowClosed] = useState(filters.includeZeroValue)

  useEffect(() => { setShowClosed(filters.includeZeroValue) }, [filters.includeZeroValue])

  useEffect(() => {
    let live = true
    setLoading(true)
    setError(null)
    api.getPortfolio({
      include_exposure: true, include_zero_value: showClosed,
      level: filters.level, group_name: filters.groupName,
      investor_name: filters.investorName, arn: filters.arn,
    }).then(p => { if (live) { setPortfolio(p); setExposure(p.exposure) } })
      .catch((err) => { if (live) setError(err.message) })
      .finally(() => { if (live) setLoading(false) })
    return () => { live = false }
  }, [filters, refreshTick, showClosed])

  if (error) return <div className="text-sm text-bad">{error}</div>
  if (loading) return <SkeletonTable rows={6} cols={4} />
  if (!portfolio) return null

  const schemes = portfolio.schemes || []
  const totals = portfolio.subtotals?.total || {}
  const currentValue = totals.current_value
  const investedValue = totals.invested_value
  const gain = totals.gain
  const gainPct = totals.absolute_return_pct

  const pieData = Object.entries(ASSET_LABELS).map(([key, name]) => ({
    name, value: portfolio.subtotals?.[key]?.known_current_value ?? 0,
  })).filter(d => d.value > 0)

  const topFunds = (exposure?.top_funds || []).slice(0, 8)

  return (
    <div className="space-y-6 animate-fade-up">
      {portfolio.data_quality === 'PARTIAL' && <p role="status" className="text-warn text-sm">Some figures need review. Valued active holdings: {totals.valued_holdings}/{totals.total_holdings}. Known value: {formatIndian(totals.known_current_value)}. Check the scheme warnings below, including closed holdings when shown.</p>}
      <div className="text-xs text-ink-3">
        Holdings include CAS transactions through {formatDate(portfolio.holdings_coverage_through)}. Valued using NAV
        available on or before {formatDate(portfolio.requested_valuation_date)}.
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-5 gap-4">
        <StatCard label="Current value" value={formatIndian(currentValue)} />
        <StatCard label="Cost of current holdings" value={formatIndian(investedValue)} />
        <StatCard label="Unrealised gain" value={formatIndian(gain)} tone={gain == null ? 'default' : gain >= 0 ? 'good' : 'bad'} />
        <StatCard label="Return on current holdings" value={formatPct(gainPct)} tone={gain == null ? 'default' : gain >= 0 ? 'good' : 'bad'} />
        <StatCard label="Lifetime XIRR" value={formatPct(totals.xirr)} />
      </div>
      <p className="text-xs text-ink-3">Cost and unrealised gain cover units still held. Lifetime XIRR includes earlier purchases and withdrawals, including fully redeemed holdings, so it can differ in sign from the return on current holdings.</p>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="rounded-xl border border-line-soft bg-card p-4">
          <div className="font-display font-semibold text-ink mb-3">Asset allocation</div>
          {pieData.length ? (
            <ResponsiveContainer width="100%" height={220}>
              <PieChart>
                <Pie data={pieData} dataKey="value" nameKey="name" innerRadius={50} outerRadius={85} paddingAngle={2}>
                  {pieData.map((_, i) => <Cell key={i} fill={CAP_COLORS[i % CAP_COLORS.length]} />)}
                </Pie>
                <Tooltip formatter={(v) => formatIndian(v)} />
              </PieChart>
            </ResponsiveContainer>
          ) : (
            <div className="text-sm text-ink-3 py-10 text-center">
              No valued holdings to allocate.
            </div>
          )}
          <div className="flex flex-wrap justify-center gap-3 text-xs text-ink-2">{pieData.map((d, i) => <span key={d.name}><span style={{ color: CAP_COLORS[i % CAP_COLORS.length] }}>●</span> {d.name} {totals.known_current_value > 0 ? `${(100 * d.value / totals.known_current_value).toFixed(1)}%` : ''}</span>)}</div>
          {currentValue == null && <p className="text-xs text-warn mt-2">Allocation covers holdings with an available valuation.</p>}
        </div>

        <div className="rounded-xl border border-line-soft bg-card p-4">
          <div className="font-display font-semibold text-ink mb-3">Top holdings</div>
          {topFunds.length ? (
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={topFunds} layout="vertical" margin={{ left: 10 }}>
                <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="#e3e0d6" />
                <XAxis type="number" tickFormatter={(v) => formatIndian(v)} fontSize={11} stroke="#8a8e9c" />
                <YAxis type="category" dataKey="scheme_name" width={140} tick={{ fontSize: 11, fill: '#4b5163' }}
                  tickFormatter={(v) => (v.length > 22 ? v.slice(0, 22) + '…' : v)} />
                <Tooltip formatter={(v) => formatIndian(v)} />
                <Bar dataKey="current_value" fill="#12172a" radius={[0, 4, 4, 0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <div className="text-sm text-ink-3 py-10 text-center">No holdings to show.</div>
          )}
        </div>
      </div>

      <div className="rounded-xl border border-line-soft bg-card overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 border-b border-line-soft">
          <div className="font-display font-semibold text-ink">{totals.total_holdings} active holdings · {totals.closed_holdings || 0} fully redeemed</div>
          <label className="flex items-center gap-2 text-xs text-ink-2"><input type="checkbox" checked={showClosed} onChange={e => setShowClosed(e.target.checked)} />Show fully redeemed holdings</label>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs uppercase tracking-wide text-ink-3 bg-paper-soft">
                <th className="text-left px-4 py-2.5">Scheme</th>
                <th className="text-left px-4 py-2.5">Advisor</th>
                <th className="text-right px-4 py-2.5">Remaining cost</th>
                <th className="text-right px-4 py-2.5">Current</th>
                <th className="text-right px-4 py-2.5">Unrealised gain</th>
                <th className="text-right px-4 py-2.5">Absolute Return</th>
                <th className="text-right px-4 py-2.5">Lifetime XIRR</th>
              </tr>
            </thead>
            <tbody>
              {schemes.map((s) => {
                // Show the actual resolved NAV date only when it differs
                // from today — a weekend/holiday gap or a data lag both
                // mean "today's NAV" isn't literally from today (spec 10.3).
                const showNavDate = s.current_nav_date && s.current_nav_date !== portfolio.requested_valuation_date
                return (
                  <tr key={s.holding_id} className="border-t border-line-soft hover:bg-paper-soft/60 transition-colors">
                    <td className="px-4 py-2.5">
                      <div className="font-medium text-ink">{s.scheme_name}</div>
                      {Number(s.balance_units) === 0 && <div className="text-xs text-ink-3">Fully redeemed</div>}
                      {(s.flags || []).map(f => <div key={f.code} className="text-xs text-warn">{f.detail || f.code}</div>)}
                      <div className="text-xs text-ink-3 font-mono">{s.folio}</div>
                    </td>
                    <td className="px-4 py-2.5 text-ink-2">{s.advisor_label || s.advisor || '—'}</td>
                    <td className="px-4 py-2.5 text-right tabular text-ink-2">{formatIndian(s.net_invested_value)}</td>
                    <td className="px-4 py-2.5 text-right tabular font-medium text-ink">
                      {formatIndian(s.current_value)}
                      {showNavDate && <div className="text-xs text-ink-3 font-normal">as of {formatDate(s.current_nav_date)}</div>}
                    </td>
                    <td className={`px-4 py-2.5 text-right tabular font-medium ${s.absolute_gain >= 0 ? 'text-good' : 'text-bad'}`}>
                      {formatIndian(s.absolute_gain)}
                    </td>
                    <td className="px-4 py-2.5 text-right tabular text-ink-2">
                      {s.absolute_gain_pct != null ? formatPct(s.absolute_gain_pct) : '—'}
                    </td>
                    <td className="px-4 py-2.5 text-right tabular text-ink-2">{s.xirr != null ? formatPct(s.xirr) : '—'}</td>
                  </tr>
                )
              })}
              {schemes.length === 0 && (
                <tr><td colSpan={7} className="px-4 py-8 text-center text-ink-3">No schemes match these filters.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
