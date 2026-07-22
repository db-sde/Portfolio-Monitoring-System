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
    <div className="rounded-xl border border-gray-200 bg-white overflow-hidden">
      <div className="text-sm font-semibold text-gray-800 px-4 py-3 border-b border-gray-100 flex items-center justify-between">
        <span>{title}</span>
        <span className="text-xs font-normal text-gray-400">{period.start_date || 'inception'} → {period.end_date}</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs uppercase tracking-wide text-gray-500 bg-gray-50">
              <th className="text-left px-4 py-2">Metric</th>
              {BUCKET_ORDER.map((b) => (
                <th key={b} className="text-right px-4 py-2">{BUCKET_LABELS[b]}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ROWS.map(([key, label]) => (
              <tr key={key} className="border-t border-gray-100">
                <td className="px-4 py-2 text-gray-600">{label}</td>
                {BUCKET_ORDER.map((b) => {
                  const v = period[b]?.[key]
                  const isXirr = key === 'xirr'
                  const isGain = key === 'net_gain'
                  const tone = (isXirr || isGain) && v != null ? (v >= 0 ? 'text-emerald-700' : 'text-red-700') : 'text-gray-900'
                  return (
                    <td key={b} className={`px-4 py-2 text-right tabular-nums font-medium ${tone}`}>
                      {isXirr ? (v != null ? formatPct(v) : '—') : formatIndian(v)}
                    </td>
                  )
                })}
              </tr>
            ))}
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
    <div className="space-y-6">
      <div className="flex flex-wrap items-end gap-4 rounded-xl border border-gray-200 bg-white p-4">
        <div>
          <label className="block text-xs font-medium text-gray-500 mb-1">Start date</label>
          <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)}
            className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm" />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-500 mb-1">End date</label>
          <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)}
            className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm" />
        </div>
        <button
          onClick={() => { setStartDate(''); setEndDate('') }}
          className="text-sm font-medium text-gray-600 hover:text-gray-900 px-3 py-1.5"
        >
          Reset to since-inception
        </button>
      </div>

      {error && <div className="text-sm text-red-600">{error}</div>}
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
