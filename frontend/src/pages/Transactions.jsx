import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { formatIndian, formatUnits, formatDate } from '../components/IndianNumber'
import SkeletonTable from '../components/SkeletonTable'

const TYPE_LABELS = {
  PURCHASE: 'Purchase', PURCHASE_SIP: 'SIP purchase', REDEMPTION: 'Redemption',
  DIVIDEND_PAYOUT: 'IDCW payout', DIVIDEND_REINVEST: 'IDCW reinvest',
  SWITCH_IN: 'Switch in', SWITCH_IN_MERGER: 'Switch in (merger)',
  SWITCH_OUT: 'Switch out', SWITCH_OUT_MERGER: 'Switch out (merger)',
  STT_TAX: 'STT', STAMP_DUTY_TAX: 'Stamp duty', TDS_TAX: 'TDS',
  SEGREGATION: 'Segregation', GIFT_IN: 'Gift in', GIFT_OUT: 'Gift out',
  MISC: 'Misc', UNKNOWN: 'Unknown', REVERSAL: 'Reversal',
}
const TYPE_GROUPS = [
  { label: 'Money in', types: ['PURCHASE', 'PURCHASE_SIP', 'DIVIDEND_REINVEST', 'SWITCH_IN', 'SWITCH_IN_MERGER', 'GIFT_IN'] },
  { label: 'Money out', types: ['REDEMPTION', 'SWITCH_OUT', 'SWITCH_OUT_MERGER', 'GIFT_OUT'] },
  { label: 'Charges & tax', types: ['STT_TAX', 'STAMP_DUTY_TAX', 'TDS_TAX'] },
  { label: 'Other', types: ['DIVIDEND_PAYOUT', 'SEGREGATION', 'REVERSAL', 'MISC', 'UNKNOWN'] },
]
const ALL_TYPES = TYPE_GROUPS.flatMap((g) => g.types)

export default function Transactions({ filters, refreshTick }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [schemeFilter, setSchemeFilter] = useState('all')
  const [typeFilter, setTypeFilter] = useState(new Set(ALL_TYPES))
  const [filterOpen, setFilterOpen] = useState(false)

  useEffect(() => {
    setLoading(true)
    setError(null)
    api.getTransactions({
      level: filters.level, group_name: filters.groupName,
      investor_name: filters.investorName, arn: filters.arn,
    })
      .then(setData)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [filters, refreshTick])

  const allTx = data?.transactions || []

  const schemeOptions = useMemo(() => {
    const seen = new Map()
    allTx.forEach((t) => { if (t.isin && !seen.has(t.isin)) seen.set(t.isin, t.scheme_name) })
    return [{ value: 'all', label: 'All schemes' }, ...[...seen.entries()].map(([isin, name]) => ({ value: isin, label: name }))]
  }, [allTx])

  const filtered = useMemo(() => {
    return allTx.filter((t) => (schemeFilter === 'all' || t.isin === schemeFilter) && typeFilter.has(t.type))
  }, [allTx, schemeFilter, typeFilter])

  if (error) return <div className="text-sm text-bad">{error}</div>
  if (loading) return <SkeletonTable rows={10} cols={7} />

  const allOn = typeFilter.size === ALL_TYPES.length

  const toggleType = (t) => {
    setTypeFilter((prev) => {
      const next = new Set(prev)
      next.has(t) ? next.delete(t) : next.add(t)
      return next
    })
  }

  return (
    <div className="space-y-4 animate-fade-up">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="text-sm text-ink-2">{filtered.length} transaction{filtered.length === 1 ? '' : 's'}</div>
        <div className="flex items-center gap-2">
          <select
            value={schemeFilter}
            onChange={(e) => setSchemeFilter(e.target.value)}
            className="rounded-lg border border-line px-3 py-1.5 text-sm outline-none focus:border-accent bg-card"
          >
            {schemeOptions.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
          <div className="relative">
            <button
              onClick={() => setFilterOpen((o) => !o)}
              className={`flex items-center gap-2 rounded-lg border px-3 py-1.5 text-sm font-medium ${
                allOn ? 'border-line text-ink-2' : 'border-accent text-accent-strong bg-accent-tint'
              }`}
            >
              Transaction type
              {!allOn && <span className="rounded-full bg-accent text-accent-ink text-xs px-1.5">{ALL_TYPES.length - typeFilter.size}</span>}
            </button>
            {filterOpen && (
              <>
                <div className="fixed inset-0 z-10" onClick={() => setFilterOpen(false)} />
                <div className="absolute right-0 z-20 mt-2 w-[560px] max-w-[90vw] rounded-xl border border-line-soft bg-card shadow-xl p-4">
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                    {TYPE_GROUPS.map((g) => (
                      <div key={g.label}>
                        <div className="text-xs font-semibold uppercase tracking-wide text-ink-3 mb-2">{g.label}</div>
                        <div className="space-y-1.5">
                          {g.types.map((t) => (
                            <label key={t} className="flex items-center gap-2 text-sm text-ink-2 cursor-pointer">
                              <input type="checkbox" checked={typeFilter.has(t)} onChange={() => toggleType(t)} className="accent-accent" />
                              {TYPE_LABELS[t] || t}
                            </label>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                  <div className="flex justify-between items-center mt-4 pt-3 border-t border-line-soft">
                    <button
                      onClick={() => setTypeFilter(allOn ? new Set() : new Set(ALL_TYPES))}
                      className="text-sm font-medium text-ink-2 hover:text-ink"
                    >
                      {allOn ? 'Unselect all' : 'Select all'}
                    </button>
                    <button onClick={() => setFilterOpen(false)} className="text-sm font-semibold text-accent-strong">Apply</button>
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      <div className="rounded-xl border border-line-soft bg-card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs uppercase tracking-wide text-ink-3 bg-paper-soft">
                <th className="text-left px-4 py-2.5">Date</th>
                <th className="text-left px-4 py-2.5">Scheme</th>
                <th className="text-left px-4 py-2.5">Type</th>
                <th className="text-right px-4 py-2.5">Amount</th>
                <th className="text-right px-4 py-2.5">Units</th>
                <th className="text-right px-4 py-2.5">NAV</th>
                <th className="text-right px-4 py-2.5">Balance</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((t, i) => (
                <tr key={i} className="border-t border-line-soft hover:bg-paper-soft/40 transition-colors">
                  <td className="px-4 py-2.5 text-ink-2 whitespace-nowrap">{formatDate(t.date)}</td>
                  <td className="px-4 py-2.5 text-ink font-medium">{t.scheme_name}</td>
                  <td className="px-4 py-2.5">
                    <span className="rounded-full bg-paper-soft px-2.5 py-0.5 text-xs font-medium text-ink-2">{TYPE_LABELS[t.type] || t.type}</span>
                  </td>
                  <td className={`px-4 py-2.5 text-right tabular font-medium ${t.amount == null ? 'text-ink-3' : t.amount < 0 ? 'text-bad' : 'text-ink'}`}>
                    {t.amount == null ? '—' : formatIndian(t.amount)}
                  </td>
                  <td className={`px-4 py-2.5 text-right tabular ${t.units == null ? 'text-ink-3' : t.units < 0 ? 'text-bad' : 'text-good'}`}>
                    {t.units == null ? '—' : formatUnits(t.units)}
                  </td>
                  <td className="px-4 py-2.5 text-right tabular text-ink-2">{t.nav == null ? '—' : Number(t.nav).toFixed(2)}</td>
                  <td className="px-4 py-2.5 text-right tabular text-ink-3">{t.balance == null ? '—' : formatUnits(t.balance)}</td>
                </tr>
              ))}
              {filtered.length === 0 && (
                <tr><td colSpan={7} className="px-4 py-8 text-center text-ink-3">No transactions match these filters.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
