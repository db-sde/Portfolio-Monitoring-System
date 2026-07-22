import { useEffect, useState } from 'react'
import { api } from '../api'
import HeatCell from '../components/HeatCell'
import SkeletonTable from '../components/SkeletonTable'

const PERIODS = ['1m', '3m', '6m', '1y', '2y', '3y']

export default function FundSummary({ filters }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    setLoading(true)
    setError(null)
    api.getFundSummary({
      include_zero_value: filters.includeZeroValue,
      level: filters.level, group_name: filters.groupName,
      investor_name: filters.investorName, arn: filters.arn,
    })
      .then(setData)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [filters])

  if (error) return <div className="text-sm text-bad">{error}</div>
  if (loading) return <SkeletonTable rows={6} cols={7} />

  const funds = data?.funds || []

  return (
    <div className="rounded-xl border border-line-soft bg-card overflow-hidden animate-fade-up">
      <div className="font-display font-semibold text-ink px-4 py-3 border-b border-line-soft">
        Fund returns heatmap ({funds.length})
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm border-separate border-spacing-y-0.5 px-2">
          <thead>
            <tr className="text-xs uppercase tracking-wide text-ink-3">
              <th className="text-left px-3 py-2">Scheme</th>
              <th className="text-right px-3 py-2">Corpus (₹Cr)</th>
              <th className="text-right px-3 py-2">Large/Mid/Small</th>
              {PERIODS.map((p) => <th key={p} className="text-right px-3 py-2">{p}</th>)}
            </tr>
          </thead>
          <tbody>
            {funds.map((f) => (
              <tr key={f.amfi}>
                <td className="px-3 py-2 font-medium text-ink whitespace-nowrap">{f.scheme_name}</td>
                <td className="px-3 py-2 text-right tabular text-ink-2">{f.corpus_cr ?? '—'}</td>
                <td className="px-3 py-2 text-right tabular text-ink-2 whitespace-nowrap">
                  {f.largecap_pct != null ? `${f.largecap_pct}/${f.midcap_pct}/${f.smallcap_pct}` : '—'}
                </td>
                {PERIODS.map((p) => <HeatCell key={p} value={f.returns?.[p]} />)}
              </tr>
            ))}
            {funds.length === 0 && (
              <tr><td colSpan={3 + PERIODS.length} className="px-4 py-8 text-center text-ink-3">No held funds match these filters.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="px-4 py-3 text-xs text-ink-3 border-t border-line-soft">
        Returns and cap-allocation data come from mfdata.in — if that source isn't reachable, these cells show "—" rather than a guess.
      </div>
    </div>
  )
}
