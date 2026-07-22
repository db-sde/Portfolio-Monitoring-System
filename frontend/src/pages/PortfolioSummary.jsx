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

  if (error) return <div className="text-sm text-red-600">{error}</div>
  if (loading) return <SkeletonTable rows={6} cols={7} />

  const groups = data?.groups || []

  return (
    <div className="space-y-6">
      {groups.map((group) => (
        <div key={group.group_name} className="space-y-4">
          <h2 className="text-sm font-semibold text-gray-800">{group.group_name}</h2>
          {group.investors.map((investor) => (
            <div key={investor.investor_name} className="rounded-xl border border-gray-200 bg-white overflow-hidden">
              <div className="px-4 py-3 border-b border-gray-100 flex items-center justify-between">
                <span className="font-medium text-gray-900">{investor.investor_name}</span>
                <span className="text-sm">
                  Blended XIRR: <span className="font-semibold tabular-nums">{investor.all_advisor_xirr != null ? formatPct(investor.all_advisor_xirr) : '—'}</span>
                </span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-xs uppercase tracking-wide text-gray-500 bg-gray-50">
                      <th className="text-left px-4 py-2">Advisor</th>
                      <th className="text-right px-4 py-2">Invested</th>
                      <th className="text-right px-4 py-2">Current</th>
                      <th className="text-right px-4 py-2">Abs. return</th>
                      <th className="text-right px-4 py-2">XIRR</th>
                      <th className="text-right px-4 py-2">L/M/S cap</th>
                      <th className="text-right px-4 py-2">Benchmark XIRR</th>
                    </tr>
                  </thead>
                  <tbody>
                    {investor.advisors.map((a) => (
                      <tr key={a.arn} className="border-t border-gray-100">
                        <td className="px-4 py-2 font-medium text-gray-900">{a.advisor_label}</td>
                        <td className="px-4 py-2 text-right tabular-nums">{formatIndian(a.investment_value)}</td>
                        <td className="px-4 py-2 text-right tabular-nums font-medium">{formatIndian(a.current_value)}</td>
                        <td className={`px-4 py-2 text-right tabular-nums ${a.absolute_return_pct >= 0 ? 'text-emerald-700' : 'text-red-700'}`}>
                          {a.absolute_return_pct != null ? formatPct(a.absolute_return_pct) : '—'}
                        </td>
                        <td className={`px-4 py-2 text-right tabular-nums font-medium ${a.xirr >= 0 ? 'text-emerald-700' : 'text-red-700'}`}>
                          {a.xirr != null ? formatPct(a.xirr) : '—'}
                        </td>
                        <td className="px-4 py-2 text-right tabular-nums text-gray-600">
                          {a.largecap_pct != null ? `${a.largecap_pct}/${a.midcap_pct}/${a.smallcap_pct}` : '—'}
                        </td>
                        <td className="px-4 py-2 text-right tabular-nums text-gray-400">
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
        <div className="text-sm text-gray-400 py-10 text-center">No groups configured yet — add one under Settings.</div>
      )}
    </div>
  )
}
