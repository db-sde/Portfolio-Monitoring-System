import { useEffect, useState } from 'react'
import { api } from '../api'
import HeatCell from '../components/HeatCell'
import SkeletonTable from '../components/SkeletonTable'
import { formatPct, formatDate } from '../components/IndianNumber'

const PERIODS = ['1m', '3m', '6m', '1y', '2y', '3y']
const STALE_NAV_DAYS = 10 // matches backend/enrichment.py's own threshold

const formatRatio = (n) => (n == null || Number.isNaN(n) ? '—' : n.toFixed(2))

// A NAV date older than this means either a genuinely delisted/frozen
// scheme, or one enrichment.py's own AMFI-recode recovery couldn't
// resolve — worth flagging in the UI rather than looking identical to a
// fresh, trustworthy figure right next to it.
const isStaleNav = (iso) => {
  if (!iso) return false
  const days = (Date.now() - new Date(iso).getTime()) / 86400000
  return days > STALE_NAV_DAYS
}

export default function FundSummary({ filters, refreshTick }) {
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
  }, [filters, refreshTick])

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
              <th className="text-right px-3 py-2">Vol (3Y)</th>
              <th className="text-right px-3 py-2">Sharpe (3Y)</th>
              <th className="text-right px-3 py-2">Sortino (3Y)</th>
              <th className="text-right px-3 py-2">Max DD</th>
              <th className="text-right px-3 py-2">Beta</th>
              <th className="text-right px-3 py-2">Alpha</th>
              <th className="text-right px-3 py-2">NAV as of</th>
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
                <td className="px-3 py-2 text-right tabular text-ink-2">{formatPct(f.risk?.std_dev)}</td>
                <td className="px-3 py-2 text-right tabular text-ink-2">{formatRatio(f.risk?.sharpe)}</td>
                <td className="px-3 py-2 text-right tabular text-ink-2">{formatRatio(f.risk?.sortino)}</td>
                <HeatCell value={f.risk?.max_drawdown} />
                <td className="px-3 py-2 text-right tabular text-ink-2">{formatRatio(f.risk?.beta)}</td>
                <HeatCell value={f.risk?.alpha} />
                <td
                  className={`px-3 py-2 text-right tabular whitespace-nowrap ${isStaleNav(f.nav_as_of) ? 'text-bad font-medium' : 'text-ink-2'}`}
                  title={isStaleNav(f.nav_as_of) ? 'This NAV looks frozen — the fund may have been merged or renamed by its AMC.' : undefined}
                >
                  {formatDate(f.nav_as_of)}
                </td>
              </tr>
            ))}
            {funds.length === 0 && (
              <tr><td colSpan={10 + PERIODS.length} className="px-4 py-8 text-center text-ink-3">No held funds match these filters.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="px-4 py-3 text-xs text-ink-3 border-t border-line-soft">
        Returns, volatility, Sharpe, Sortino, max drawdown, and alpha/beta are computed from mfapi.in's NAV history (mfdata.in has been unreachable) — beta/alpha use a Nifty 50 index fund as a benchmark proxy, since no free source publishes raw index values. "NAV as of" is the latest date mfapi.in has for that scheme; if it looks frozen in the past (highlighted), the fund's AMFI code was likely retired by an AMC merger/rename — enrichment tries to recover the current code by ISIN automatically, but flags it here if that didn't resolve. Cap-allocation still needs a source we don't have, so those cells show "—" rather than a guess.
      </div>
    </div>
  )
}
