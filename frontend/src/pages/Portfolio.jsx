import { Fragment, useEffect, useState } from 'react'
import { api } from '../api'
import { formatIndian, formatPct, formatUnits } from '../components/IndianNumber'
import SkeletonTable from '../components/SkeletonTable'

const CAT_ORDER = ['EQUITY', 'DEBT', 'OTHER']
const CAT_LABEL = { EQUITY: 'Equity', DEBT: 'Debt', OTHER: 'Other' }

function normalizeCat(assetClass) {
  const t = (assetClass || '').toUpperCase()
  return t === 'EQUITY' || t === 'DEBT' ? t : 'OTHER'
}

function Row({ r }) {
  return (
    <tr className="border-t border-line-soft hover:bg-paper-soft/40 transition-colors">
      <td className="px-4 py-2.5 font-mono text-xs text-ink-3">{r.folio}</td>
      <td className="px-4 py-2.5">
        <div className="font-medium text-ink">{r.scheme_name}</div>
        <div className="text-xs text-ink-3 font-mono">{r.isin || ''}</div>
      </td>
      <td className="px-4 py-2.5 text-right tabular text-ink-2">{formatUnits(r.balance_units)}</td>
      <td className="px-4 py-2.5 text-right tabular text-ink-2">{r.weighted_purchase_nav != null ? Number(r.weighted_purchase_nav).toFixed(4) : '—'}</td>
      <td className="px-4 py-2.5 text-right tabular text-ink-2">{r.current_nav != null ? Number(r.current_nav).toFixed(4) : '—'}</td>
      <td className="px-4 py-2.5 text-right tabular text-ink-2">{formatIndian(r.net_invested_value)}</td>
      <td className="px-4 py-2.5 text-right tabular font-medium text-ink">{formatIndian(r.current_value)}</td>
      <td className={`px-4 py-2.5 text-right tabular font-semibold ${r.absolute_gain >= 0 ? 'text-good' : 'text-bad'}`}>{formatIndian(r.absolute_gain)}</td>
      <td className="px-4 py-2.5 text-right tabular text-ink-3">{r.weighted_days_held ?? '—'}</td>
      <td className="px-4 py-2.5 text-right tabular text-ink-2">{r.absolute_gain_pct != null ? formatPct(r.absolute_gain_pct) : '—'}</td>
      <td className="px-4 py-2.5 text-right tabular text-ink-2">{r.xirr != null ? formatPct(r.xirr) : '—'}</td>
    </tr>
  )
}

function SubtotalRow({ label, s, grand = false }) {
  if (!s) return null
  return (
    <tr className={grand ? 'border-t-2 border-ink/20 bg-paper-soft font-semibold' : 'border-t border-line-soft bg-paper-soft/60 font-medium'}>
      <td className="px-4 py-2.5 text-ink" colSpan={2}>{label}</td>
      <td className="px-4 py-2.5" />
      <td className="px-4 py-2.5" />
      <td className="px-4 py-2.5" />
      <td className="px-4 py-2.5 text-right tabular text-ink-2">{formatIndian(s.invested_value)}</td>
      <td className="px-4 py-2.5 text-right tabular text-ink">{formatIndian(s.current_value)}</td>
      <td className={`px-4 py-2.5 text-right tabular ${s.gain >= 0 ? 'text-good' : 'text-bad'}`}>{formatIndian(s.gain)}</td>
      <td className="px-4 py-2.5 text-right tabular text-ink-3">{s.weighted_days_held ?? '—'}</td>
      <td className="px-4 py-2.5 text-right tabular text-ink-2">{s.absolute_return_pct != null ? formatPct(s.absolute_return_pct) : '—'}</td>
      <td className="px-4 py-2.5 text-right tabular text-ink-2">{s.xirr != null ? formatPct(s.xirr) : '—'}</td>
    </tr>
  )
}

export default function Portfolio({ filters, refreshTick }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    setLoading(true)
    setError(null)
    api.getPortfolio({
      include_zero_value: filters.includeZeroValue,
      level: filters.level, group_name: filters.groupName,
      investor_name: filters.investorName, arn: filters.arn,
    })
      .then(setData)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [filters, refreshTick])

  if (error) return <div className="text-sm text-bad">{error}</div>
  if (loading) return <SkeletonTable rows={8} cols={7} />
  if (!data) return null

  const rows = (data.schemes || []).map((s) => ({ ...s, cat: normalizeCat(s.asset_class) }))
  const subtotals = data.subtotals || {}

  const byCat = {}
  rows.forEach((r) => { (byCat[r.cat] = byCat[r.cat] || []).push(r) })

  if (!rows.length) {
    return (
      <div className="rounded-xl border border-line-soft bg-card p-10 text-center text-sm text-ink-3">
        No holdings match these filters.
      </div>
    )
  }

  return (
    <div className="rounded-xl border border-line-soft bg-card overflow-hidden animate-fade-up">
      <div className="font-display font-semibold text-ink px-4 py-3 border-b border-line-soft">
        Portfolio ({rows.length} scheme{rows.length === 1 ? '' : 's'})
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs uppercase tracking-wide text-ink-3 bg-paper-soft">
              <th className="text-left px-4 py-2.5">Folio</th>
              <th className="text-left px-4 py-2.5">Scheme</th>
              <th className="text-right px-4 py-2.5">Balance units</th>
              <th className="text-right px-4 py-2.5">Weighted purchase NAV</th>
              <th className="text-right px-4 py-2.5">Current NAV</th>
              <th className="text-right px-4 py-2.5">Purchase value</th>
              <th className="text-right px-4 py-2.5">Current value</th>
              <th className="text-right px-4 py-2.5">Gain</th>
              <th className="text-right px-4 py-2.5">Weighted days held</th>
              <th className="text-right px-4 py-2.5">Abs. return</th>
              <th className="text-right px-4 py-2.5">XIRR</th>
            </tr>
          </thead>
          <tbody>
            {CAT_ORDER.filter((cat) => byCat[cat]?.length).map((cat) => (
              <Fragment key={cat}>
                <tr>
                  <td colSpan={11} className="px-4 pt-4 pb-1 text-xs font-semibold uppercase tracking-wide text-ink-3">
                    {CAT_LABEL[cat]}
                  </td>
                </tr>
                {byCat[cat].map((r) => <Row key={r.holding_id} r={r} />)}
                <SubtotalRow label={`Subtotal — ${CAT_LABEL[cat]}`} s={subtotals[cat]} />
              </Fragment>
            ))}
            <SubtotalRow label="Grand total" s={subtotals.total} grand />
          </tbody>
        </table>
      </div>
      <div className="px-4 py-3 text-xs text-ink-3 border-t border-line-soft">
        Purchase value, current value, and current NAV all use MFAPI-resolved NAV — the CAS statement's own printed
        valuation is never used for these figures. XIRR uses each holding's full dated cash-flow history, not CAGR.
      </div>
    </div>
  )
}
