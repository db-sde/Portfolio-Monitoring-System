import { Fragment, useEffect, useState } from 'react'
import { api } from '../api'
import { formatIndian, formatPct, formatUnits } from '../components/IndianNumber'
import SkeletonTable from '../components/SkeletonTable'

const DAY_MS = 86400000
const CAT_ORDER = ['EQUITY', 'DEBT', 'OTHER']
const CAT_LABEL = { EQUITY: 'Equity', DEBT: 'Debt', OTHER: 'Other' }

// scheme.type only ever resolves to "EQUITY" or "DEBT" from casparser's
// own ISIN lookup — anything else (unmatched schemes come back "N/A", not
// "UNKNOWN") still needs a home, so every other value folds into "Other"
// rather than being silently dropped from the table.
function normalizeCat(type) {
  const t = (type || '').toUpperCase()
  return t === 'EQUITY' || t === 'DEBT' ? t : 'OTHER'
}

function firstPurchaseDate(transactions) {
  let min = null
  for (const t of transactions || []) {
    if ((t.type === 'PURCHASE' || t.type === 'PURCHASE_SIP') && t.date) {
      const d = new Date(t.date)
      if (!Number.isNaN(d.getTime()) && (!min || d < min)) min = d
    }
  }
  return min
}

function cagr(cost, value, days) {
  if (cost > 0 && days > 0) return (Math.pow(value / cost, 365 / days) - 1) * 100
  if (cost > 0) return ((value - cost) / cost) * 100
  return 0
}

function buildRows(schemes, asOf) {
  return schemes.map((s) => {
    const cost = s.cost_value || 0
    const value = s.current_value || 0
    const buy = firstPurchaseDate(s.transactions)
    const days = buy ? Math.max(1, Math.round((asOf - buy) / DAY_MS)) : 0
    const purchaseNav = s.current_units ? cost / s.current_units : 0
    const gain = value - cost
    const absPct = cost ? (gain / cost) * 100 : 0
    return {
      cat: normalizeCat(s.type),
      folio: s.folio,
      scheme_name: s.scheme_name,
      isin: s.isin,
      units: s.current_units,
      purchaseNav,
      currentNav: s.current_nav,
      cost,
      value,
      gain,
      days,
      absPct,
      cagrPct: cagr(cost, value, days),
    }
  })
}

function subtotal(rows, label) {
  let cost = 0, value = 0, weightedDays = 0
  rows.forEach((r) => { cost += r.cost; value += r.value; weightedDays += r.days * r.cost })
  const days = cost ? Math.round(weightedDays / cost) : 0
  const gain = value - cost
  const absPct = cost ? (gain / cost) * 100 : 0
  return { label, cost, value, gain, days, absPct, cagrPct: cagr(cost, value, days) }
}

function Row({ r }) {
  return (
    <tr className="border-t border-line-soft hover:bg-paper-soft/40 transition-colors">
      <td className="px-4 py-2.5 font-mono text-xs text-ink-3">{r.folio}</td>
      <td className="px-4 py-2.5">
        <div className="font-medium text-ink">{r.scheme_name}</div>
        <div className="text-xs text-ink-3 font-mono">{r.isin || ''}</div>
      </td>
      <td className="px-4 py-2.5 text-right tabular text-ink-2">{formatUnits(r.units)}</td>
      <td className="px-4 py-2.5 text-right tabular text-ink-2">{r.purchaseNav.toFixed(4)}</td>
      <td className="px-4 py-2.5 text-right tabular text-ink-2">{Number(r.currentNav || 0).toFixed(4)}</td>
      <td className="px-4 py-2.5 text-right tabular text-ink-2">{formatIndian(r.cost)}</td>
      <td className="px-4 py-2.5 text-right tabular font-medium text-ink">{formatIndian(r.value)}</td>
      <td className={`px-4 py-2.5 text-right tabular font-semibold ${r.gain >= 0 ? 'text-good' : 'text-bad'}`}>{formatIndian(r.gain)}</td>
      <td className="px-4 py-2.5 text-right tabular text-ink-3">{r.days}</td>
      <td className="px-4 py-2.5 text-right tabular text-ink-2">{r.absPct.toFixed(2)}%</td>
      <td className="px-4 py-2.5 text-right tabular text-ink-2">{r.cagrPct.toFixed(2)}%</td>
    </tr>
  )
}

function SubtotalRow({ s, grand = false }) {
  return (
    <tr className={grand ? 'border-t-2 border-ink/20 bg-paper-soft font-semibold' : 'border-t border-line-soft bg-paper-soft/60 font-medium'}>
      <td className="px-4 py-2.5 text-ink" colSpan={2}>{s.label}</td>
      <td className="px-4 py-2.5" />
      <td className="px-4 py-2.5" />
      <td className="px-4 py-2.5" />
      <td className="px-4 py-2.5 text-right tabular text-ink-2">{formatIndian(s.cost)}</td>
      <td className="px-4 py-2.5 text-right tabular text-ink">{formatIndian(s.value)}</td>
      <td className={`px-4 py-2.5 text-right tabular ${s.gain >= 0 ? 'text-good' : 'text-bad'}`}>{formatIndian(s.gain)}</td>
      <td className="px-4 py-2.5 text-right tabular text-ink-3">{s.days}</td>
      <td className="px-4 py-2.5 text-right tabular text-ink-2">{s.absPct.toFixed(2)}%</td>
      <td className="px-4 py-2.5 text-right tabular text-ink-2">{s.cagrPct.toFixed(2)}%</td>
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

  const schemes = data.schemes || []
  const asOf = data.statement_period?.to ? new Date(data.statement_period.to) : new Date()
  const rows = buildRows(schemes, asOf)

  const byCat = {}
  rows.forEach((r) => { (byCat[r.cat] = byCat[r.cat] || []).push(r) })

  if (!rows.length) {
    return (
      <div className="rounded-xl border border-line-soft bg-card p-10 text-center text-sm text-ink-3">
        No holdings match these filters.
      </div>
    )
  }

  const grand = subtotal(rows, 'Grand total')

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
              <th className="text-right px-4 py-2.5">Purchase NAV</th>
              <th className="text-right px-4 py-2.5">Current NAV</th>
              <th className="text-right px-4 py-2.5">Purchase value</th>
              <th className="text-right px-4 py-2.5">Current value</th>
              <th className="text-right px-4 py-2.5">Gain</th>
              <th className="text-right px-4 py-2.5">Days held</th>
              <th className="text-right px-4 py-2.5">Abs. return</th>
              <th className="text-right px-4 py-2.5">CAGR</th>
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
                {byCat[cat].map((r) => <Row key={r.folio + r.scheme_name + r.isin} r={r} />)}
                <SubtotalRow s={subtotal(byCat[cat], `Subtotal — ${CAT_LABEL[cat]}`)} />
              </Fragment>
            ))}
            <SubtotalRow s={grand} grand />
          </tbody>
        </table>
      </div>
    </div>
  )
}
