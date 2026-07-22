export const formatIndian = (n) => {
  if (n == null || Number.isNaN(n)) return '—'
  const isNeg = n < 0
  const abs = Math.abs(Math.round(n)).toString()
  let result = abs.slice(-3)
  let rest = abs.slice(0, -3)
  while (rest.length > 2) {
    result = rest.slice(-2) + ',' + result
    rest = rest.slice(0, -2)
  }
  if (rest) result = rest + ',' + result
  return (isNeg ? '−' : '') + '₹' + result
}

export const formatPct = (n, digits = 2) => {
  if (n == null || Number.isNaN(n)) return '—'
  const sign = n >= 0 ? '+' : ''
  return `${sign}${n.toFixed(digits)}%`
}

export default function IndianNumber({ value, className = '' }) {
  return <span className={className}>{formatIndian(value)}</span>
}
