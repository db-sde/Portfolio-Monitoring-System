import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { formatIndian, formatUnits, formatDate } from '../components/IndianNumber'
import SkeletonTable from '../components/SkeletonTable'

export default function CapitalGains({ filters, refreshTick }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [fy, setFy] = useState(null)
  const [exporting, setExporting] = useState(false)

  useEffect(() => {
    setLoading(true)
    setError(null)
    api.getCapitalGains({
      level: filters.level, group_name: filters.groupName,
      investor_name: filters.investorName, arn: filters.arn,
    })
      .then((d) => { setData(d); setFy((prev) => (prev && d.fys.includes(prev) ? prev : d.fys[0] || null)) })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [filters, refreshTick])

  const gains = data?.gains || []
  const gifts = data?.gifts || []
  const fyRows = useMemo(() => gains.filter((g) => g.fy === fy), [gains, fy])

  // Spec 12.1: every summary card scopes to the SELECTED FY, not every
  // year ever realised — computed from fyRows, not the full gains list.
  const { stcg, ltcg, net } = useMemo(() => {
    let s = 0, l = 0
    fyRows.forEach((g) => { s += g.stcg; l += g.ltcg })
    return { stcg: s, ltcg: l, net: s + l }
  }, [fyRows])

  if (error) return <div className="text-sm text-bad">{error}</div>
  if (loading) return <SkeletonTable rows={8} cols={7} />

  const exportFyCsv = async () => {
    setExporting(true)
    try {
      const blob = await api.download112aCsv({
        fy, level: filters.level, group_name: filters.groupName,
        investor_name: filters.investorName, arn: filters.arn,
      })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `capital-gains-112a-${fy}.csv`
      document.body.appendChild(a)
      a.click()
      a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } finally {
      setExporting(false)
    }
  }

  return (
    <div className="space-y-6 animate-fade-up">
      {(data?.warnings || []).map((w, i) => (
        <div key={i} className="rounded-lg border border-warn/20 bg-warn-tint text-warn text-sm px-4 py-2.5">{w}</div>
      ))}

      {!gains.length ? (
        <div className="rounded-xl border border-line-soft bg-card p-10 text-center text-sm text-ink-3">
          No realised sales found — gains only appear once units are actually redeemed or switched out.
        </div>
      ) : (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <div className="rounded-xl border border-line-soft bg-card p-4">
              <div className="text-xs font-medium text-ink-3 mb-1">Short-term (STCG)</div>
              <div className={`font-display text-2xl font-bold tabular ${stcg >= 0 ? 'text-good' : 'text-bad'}`}>{formatIndian(stcg)}</div>
            </div>
            <div className="rounded-xl border border-line-soft bg-card p-4">
              <div className="text-xs font-medium text-ink-3 mb-1">Long-term (LTCG)</div>
              <div className={`font-display text-2xl font-bold tabular ${ltcg >= 0 ? 'text-good' : 'text-bad'}`}>{formatIndian(ltcg)}</div>
            </div>
            <div className="rounded-xl border border-band-line bg-band p-4">
              <div className="text-xs font-medium text-band-ink/70 mb-1">Net realised gain (selected FY)</div>
              <div className="font-display text-2xl font-bold tabular text-band-ink">{formatIndian(net)}</div>
            </div>
          </div>

          <div className="rounded-xl border border-line-soft bg-card overflow-hidden">
            <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 border-b border-line-soft">
              <div>
                <div className="font-display font-semibold text-ink">Realised transactions</div>
                <div className="text-xs text-ink-3">Schedule 112A format</div>
              </div>
              <div className="flex items-center gap-2">
                <select
                  value={fy || ''}
                  onChange={(e) => setFy(e.target.value)}
                  className="rounded-lg border border-line px-3 py-1.5 text-sm outline-none focus:border-accent bg-card"
                >
                  {data.fys.map((f) => <option key={f} value={f}>FY {f}</option>)}
                </select>
                <button
                  onClick={exportFyCsv}
                  disabled={exporting}
                  className="rounded-lg bg-accent text-accent-ink text-sm font-medium px-3 py-1.5 hover:bg-accent-strong transition-colors disabled:opacity-50"
                >
                  {exporting ? 'Exporting…' : 'Export 112A CSV'}
                </button>
              </div>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs uppercase tracking-wide text-ink-3 bg-paper-soft">
                    <th className="text-left px-4 py-2.5">Scheme</th>
                    <th className="text-left px-4 py-2.5">Acquired</th>
                    <th className="text-left px-4 py-2.5">Sold</th>
                    <th className="text-right px-4 py-2.5">Units</th>
                    <th className="text-right px-4 py-2.5">Cost</th>
                    <th className="text-right px-4 py-2.5">Sale value</th>
                    <th className="text-right px-4 py-2.5">Gain / loss</th>
                  </tr>
                </thead>
                <tbody>
                  {fyRows.map((g, i) => (
                    <tr key={i} className="border-t border-line-soft hover:bg-paper-soft/40 transition-colors">
                      <td className="px-4 py-2.5">
                        <div className="font-medium text-ink">{g.scheme}</div>
                        <span className={`text-xs font-medium ${g.gain_type === 'LTCG' ? 'text-accent-strong' : 'text-ink-3'}`}>{g.gain_type}</span>
                      </td>
                      <td className="px-4 py-2.5 text-ink-2 whitespace-nowrap">{formatDate(g.purchase_date)}</td>
                      <td className="px-4 py-2.5 text-ink-2 whitespace-nowrap">{formatDate(g.sale_date)}</td>
                      <td className="px-4 py-2.5 text-right tabular text-ink-2">{formatUnits(g.units)}</td>
                      <td className="px-4 py-2.5 text-right tabular text-ink-2">{formatIndian(g.acquisition_value)}</td>
                      <td className="px-4 py-2.5 text-right tabular font-medium text-ink">{formatIndian(g.sale_value)}</td>
                      <td className={`px-4 py-2.5 text-right tabular font-semibold ${g.gain >= 0 ? 'text-good' : 'text-bad'}`}>{formatIndian(g.gain)}</td>
                    </tr>
                  ))}
                  {fyRows.length === 0 && (
                    <tr><td colSpan={7} className="px-4 py-8 text-center text-ink-3">No realised sales in FY {fy}.</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          {gifts.length > 0 && (
            <div className="rounded-xl border border-line-soft bg-card overflow-hidden">
              <div className="px-4 py-3 border-b border-line-soft">
                <div className="font-display font-semibold text-ink">Gift transfers</div>
                <div className="text-xs text-ink-3">Informational only — not part of the gains totals above</div>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-xs uppercase tracking-wide text-ink-3 bg-paper-soft">
                      <th className="text-left px-4 py-2.5">Scheme</th>
                      <th className="text-left px-4 py-2.5">Direction</th>
                      <th className="text-left px-4 py-2.5">Date</th>
                      <th className="text-right px-4 py-2.5">Units</th>
                      <th className="text-right px-4 py-2.5">Value</th>
                      <th className="text-left px-4 py-2.5">Counterparty folio</th>
                    </tr>
                  </thead>
                  <tbody>
                    {gifts.map((g, i) => (
                      <tr key={i} className="border-t border-line-soft">
                        <td className="px-4 py-2.5 font-medium text-ink">{g.scheme}</td>
                        <td className="px-4 py-2.5 text-ink-2">{g.direction === 'IN' ? 'Received' : 'Given'}</td>
                        <td className="px-4 py-2.5 text-ink-2 whitespace-nowrap">{formatDate(g.date)}</td>
                        <td className="px-4 py-2.5 text-right tabular text-ink-2">{formatUnits(g.units)}</td>
                        <td className="px-4 py-2.5 text-right tabular text-ink-2">{formatIndian(g.value)}</td>
                        <td className="px-4 py-2.5 text-ink-3 font-mono text-xs">{g.counterparty_folio || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
