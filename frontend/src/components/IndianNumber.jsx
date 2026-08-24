export const formatIndian = (n) => {
  if (n == null || Number.isNaN(n)) return '—'
  // Round the magnitude first, then decide the sign from that — a
  // genuine-but-tiny loss like -0.4 must read as "₹0", not "−₹0": the
  // same negative-zero display bug xirr_engine.py explicitly guards
  // against for XIRR, just as real here for any other money figure.
  const rounded = Math.round(Math.abs(n))
  const digits = rounded.toString()
  let result = digits.slice(-3)
  let rest = digits.slice(0, -3)
  while (rest.length > 2) {
    result = rest.slice(-2) + ',' + result
    rest = rest.slice(0, -2)
  }
  if (rest) result = rest + ',' + result
  const isNeg = n < 0 && rounded !== 0
  return (isNeg ? '−' : '') + '₹' + result
}

export const formatPct = (n, digits = 2) => {
  if (n == null || Number.isNaN(n)) return '—'
  // Same negative-zero guard as formatIndian: a value that rounds to
  // 0.00 at this precision must show as "0.00%", never "-0.00%" —
  // reads as a bug otherwise, even though the underlying number really
  // is a hair below zero.
  const magnitude = Math.abs(n).toFixed(digits)
  if (Number(magnitude) === 0) return `${magnitude}%`
  const sign = n > 0 ? '+' : '-'
  return `${sign}${magnitude}%`
}

export const formatUnits = (n) => {
  if (n == null || Number.isNaN(n)) return '—'
  return Number(n).toLocaleString('en-IN', { minimumFractionDigits: 3, maximumFractionDigits: 3 })
}

export const formatDate = (iso) => {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' })
}

// Every fund/NAV timestamp this app shows is meaningful in IST — the
// enriched-at time in particular is about India-market data freshness,
// not the viewer's own clock. toLocaleString() with no timeZone uses
// whatever the browser's system timezone happens to be, which silently
// shows the wrong wall-clock time for anyone not already on IST.
export const formatDateTimeIST = (iso) => {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', year: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: true,
  }) + ' IST'
}

export default function IndianNumber({ value, className = '' }) {
  return <span className={className}>{formatIndian(value)}</span>
}
