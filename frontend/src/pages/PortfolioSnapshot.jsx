import { useEffect, useState } from 'react'
import { api } from '../api'
import { formatIndian, formatPct } from '../components/IndianNumber'
import SkeletonTable from '../components/SkeletonTable'

const BUCKET_LABELS = { EQUITY: 'Equity', HYBRID: 'Hybrid', DEBT: 'Debt', total: 'Total' }
const BUCKET_ORDER = ['EQUITY', 'HYBRID', 'DEBT', 'total']
const ROWS = [
  ['opening_balance', 'Opening balance'],
  ['purchase', 'Purchase'],
  ['switch_in', 'Switch in'],
  ['switch_out', 'Switch out'],
  ['div_payout', 'Dividend payout'],
  ['redemption', 'Redemption'],
  ['net_addition', 'Net addition'],
  ['closing_balance', 'Closing balance'],
  ['net_gain', 'Net gain'],
  ['xirr', 'XIRR'],
]

function SnapshotTable({ title, period }) {
  if (!period) return null
  return (
    <div className="rounded-xl border border-line-soft bg-card overflow-hidden">
      <div className="font-display font-semibold text-ink px-4 py-3 border-b border-line-soft flex items-center justify-between">
        <span>{title}</span>
        <span className="text-xs font-normal text-ink-3 font-sans">{period.start_date || 'inception'} → {period.end_date}</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs uppercase tracking-wide text-ink-3 bg-paper-soft">
              <th className="text-left px-4 py-2.5">Metric</th>
              {BUCKET_ORDER.map((b) => (
                <th key={b} className="text-right px-4 py-2.5">{BUCKET_LABELS[b]}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ROWS.map(([key, label], idx) => {
              const isTotalRow = key === 'net_gain' || key === 'xirr'
              return (
                <tr key={key} className={`border-t border-line-soft ${isTotalRow ? 'bg-paper-soft/50' : ''} ${idx === ROWS.length - 1 ? 'border-b border-line-soft' : ''}`}>
                  <td className="px-4 py-2.5 text-ink-2">{label}</td>
                  {BUCKET_ORDER.map((b) => {
                    const v = period[b]?.[key]
                    const isXirr = key === 'xirr'
                    const isGain = key === 'net_gain'
                    const tone = (isXirr || isGain) && v != null ? (v >= 0 ? 'text-good' : 'text-bad') : 'text-ink'
                    return (
                      <td key={b} className={`px-4 py-2.5 text-right tabular font-medium ${tone}`}>
                        {isXirr ? (v != null ? formatPct(v) : '—') : formatIndian(v)}
                      </td>
                    )
                  })}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export default function PortfolioSnapshot({ filters }) {
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    setLoading(true)
    setError(null)
    api.getSnapshot({
      start_date: startDate || undefined,
      end_date: endDate || undefined,
      level: filters.level, group_name: filters.groupName,
      investor_name: filters.investorName, arn: filters.arn,
    })
      .then(setData)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [startDate, endDate, filters])

  return (
    <div className="space-y-6 animate-fade-up">
      <div className="flex flex-wrap items-end gap-4 rounded-xl border border-line-soft bg-card p-4">
        <div>
          <label className="block text-xs font-medium text-ink-3 mb-1">Start date</label>
          <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)}
            className="rounded-lg border border-line px-3 py-1.5 text-sm outline-none focus:border-accent" />
        </div>
        <div>
          <label className="block text-xs font-medium text-ink-3 mb-1">End date</label>
          <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)}
            className="rounded-lg border border-line px-3 py-1.5 text-sm outline-none focus:border-accent" />
        </div>
        <button
          onClick={() => { setStartDate(''); setEndDate('') }}
          className="text-sm font-medium text-ink-2 hover:text-ink px-3 py-1.5"
        >
          Reset to since-inception
        </button>
      </div>

      {error && <div className="text-sm text-bad">{error}</div>}
      {loading ? (
        <SkeletonTable rows={10} cols={4} />
      ) : data ? (
        <div className="space-y-6">
          <SnapshotTable title="Selected period" period={data.given_period} />
          <SnapshotTable title="Since inception" period={data.since_inception} />
        </div>
      ) : null}
    </div>
  )
}
