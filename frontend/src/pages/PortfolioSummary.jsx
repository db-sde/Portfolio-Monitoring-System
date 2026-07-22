import { useEffect, useState } from 'react'
import { api } from '../api'
import { formatIndian, formatPct } from '../components/IndianNumber'
import SkeletonTable from '../components/SkeletonTable'

export default function PortfolioSummary({ refreshTick }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    setLoading(true)
    setError(null)
    api.getPortfolioSummary().then(setData).catch((err) => setError(err.message)).finally(() => setLoading(false))
  }, [refreshTick])

  if (error) return <div className="text-sm text-bad">{error}</div>
  if (loading) return <SkeletonTable rows={6} cols={7} />

  const groups = data?.groups || []

  return (
    <div className="space-y-6 animate-fade-up">
      {groups.map((group) => (
        <div key={group.group_name} className="space-y-4">
          <h2 className="font-display font-semibold text-ink">{group.group_name}</h2>
          {group.investors.map((investor) => (
            <div key={investor.investor_name} className="rounded-xl border border-line-soft bg-card overflow-hidden">
              <div className="px-4 py-3 border-b border-line-soft flex items-center justify-between bg-paper-soft/40">
                <span className="font-semibold text-ink">{investor.investor_name}</span>
                <span className="text-sm text-ink-2">
                  Blended XIRR: <span className="font-semibold tabular text-ink">{investor.all_advisor_xirr != null ? formatPct(investor.all_advisor_xirr) : '—'}</span>
                </span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-xs uppercase tracking-wide text-ink-3 bg-paper-soft">
                      <th className="text-left px-4 py-2.5">Advisor</th>
                      <th className="text-right px-4 py-2.5">Invested</th>
                      <th className="text-right px-4 py-2.5">Current</th>
                      <th className="text-right px-4 py-2.5">Abs. return</th>
                      <th className="text-right px-4 py-2.5">XIRR</th>
                      <th className="text-right px-4 py-2.5">L/M/S cap</th>
                      <th className="text-right px-4 py-2.5">Benchmark XIRR</th>
                    </tr>
                  </thead>
                  <tbody>
                    {investor.advisors.map((a) => (
                      <tr key={a.arn} className="border-t border-line-soft hover:bg-paper-soft/40 transition-colors">
                        <td className="px-4 py-2.5 font-medium text-ink">{a.advisor_label}</td>
                        <td className="px-4 py-2.5 text-right tabular text-ink-2">{formatIndian(a.investment_value)}</td>
                        <td className="px-4 py-2.5 text-right tabular font-medium text-ink">{formatIndian(a.current_value)}</td>
                        <td className={`px-4 py-2.5 text-right tabular ${a.absolute_return_pct >= 0 ? 'text-good' : 'text-bad'}`}>
                          {a.absolute_return_pct != null ? formatPct(a.absolute_return_pct) : '—'}
                        </td>
                        <td className={`px-4 py-2.5 text-right tabular font-medium ${a.xirr >= 0 ? 'text-good' : 'text-bad'}`}>
                          {a.xirr != null ? formatPct(a.xirr) : '—'}
                        </td>
                        <td className="px-4 py-2.5 text-right tabular text-ink-2">
                          {a.largecap_pct != null ? `${a.largecap_pct}/${a.midcap_pct}/${a.smallcap_pct}` : '—'}
                        </td>
                        <td className="px-4 py-2.5 text-right tabular text-ink-3">
                          {a.benchmark_xirr != null ? formatPct(a.benchmark_xirr) : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </div>
      ))}
      {groups.length === 0 && (
        <div className="text-sm text-ink-3 py-10 text-center">No groups configured yet — add one under Settings.</div>
      )}
    </div>
  )
}
