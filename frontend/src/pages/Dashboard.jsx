import { useEffect, useState } from 'react'
import { api } from '../api'
import { formatIndian, formatPct, formatDate } from '../components/IndianNumber'
import SkeletonTable from '../components/SkeletonTable'
import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip, BarChart, Bar, XAxis, YAxis, CartesianGrid } from 'recharts'

const CAP_COLORS = ['#12172a', '#a9762f', '#c9b08a']

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

  useEffect(() => {
    setLoading(true)
    setError(null)
    Promise.all([
      api.getPortfolio({
        include_zero_value: filters.includeZeroValue,
        level: filters.level, group_name: filters.groupName,
        investor_name: filters.investorName, arn: filters.arn,
      }),
      api.getExposure().catch(() => null),
    ])
      .then(([p, e]) => { setPortfolio(p); setExposure(e) })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [filters, refreshTick])

  if (error) return <div className="text-sm text-bad">{error}</div>
  if (loading) return <SkeletonTable rows={6} cols={4} />
  if (!portfolio) return null

  const schemes = portfolio.schemes || []
  // current_value/absolute_gain/xirr are ALWAYS live now (CAS units x
  // current MFAPI NAV) — there is no "as of statement" figure anymore.
  // The CAS statement's own valuation.value is never read for this.
  const currentValue = schemes.reduce((s, r) => s + Number(r.current_value || 0), 0)
  // net_invested_value: cost basis of units still held (open FIFO lots),
  // 0 for a fully-redeemed holding — never the gross lifetime-invested figure.
  const investedValue = schemes.reduce((s, r) => s + Number(r.net_invested_value || 0), 0)
  const gain = currentValue - investedValue
  const gainPct = investedValue ? (gain / investedValue) * 100 : null

  const capData = exposure?.cap_allocation
  const pieData = capData
    ? [
        { name: 'Large cap', value: capData.largecap_pct },
        { name: 'Mid cap', value: capData.midcap_pct },
        { name: 'Small cap', value: capData.smallcap_pct },
      ].filter((d) => d.value != null && d.value > 0)
    : []

  const topFunds = (exposure?.top_funds || []).slice(0, 8)

  return (
    <div className="space-y-6 animate-fade-up">
      <div className="text-xs text-ink-3">
        Holdings include CAS transactions through {formatDate(portfolio.holdings_coverage_through)}. Valued using NAV
        available on or before {formatDate(portfolio.requested_valuation_date)}.
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard label="Current value" value={formatIndian(currentValue)} />
        <StatCard label="Invested value" value={formatIndian(investedValue)} />
        <StatCard label="Overall gain" value={formatIndian(gain)} tone={gain >= 0 ? 'good' : 'bad'} />
        <StatCard label="Absolute return" value={formatPct(gainPct)} tone={gain >= 0 ? 'good' : 'bad'} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="rounded-xl border border-line-soft bg-card p-4">
          <div className="font-display font-semibold text-ink mb-3">Cap allocation</div>
          {pieData.length ? (
            <ResponsiveContainer width="100%" height={220}>
              <PieChart>
                <Pie data={pieData} dataKey="value" nameKey="name" innerRadius={50} outerRadius={85} paddingAngle={2}>
                  {pieData.map((_, i) => <Cell key={i} fill={CAP_COLORS[i % CAP_COLORS.length]} />)}
                </Pie>
                <Tooltip formatter={(v) => `${v.toFixed(1)}%`} />
              </PieChart>
            </ResponsiveContainer>
          ) : (
            <div className="text-sm text-ink-3 py-10 text-center">
              Market-cap allocation unavailable — no free data source currently provides real portfolio-holdings/cap data.
            </div>
          )}
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
        <div className="font-display font-semibold text-ink px-4 py-3 border-b border-line-soft">Schemes ({schemes.length})</div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs uppercase tracking-wide text-ink-3 bg-paper-soft">
                <th className="text-left px-4 py-2.5">Scheme</th>
                <th className="text-left px-4 py-2.5">Advisor</th>
                <th className="text-right px-4 py-2.5">Invested</th>
                <th className="text-right px-4 py-2.5">Current</th>
                <th className="text-right px-4 py-2.5">Gain</th>
                <th className="text-right px-4 py-2.5">Absolute Return</th>
                <th className="text-right px-4 py-2.5">XIRR</th>
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
