import { formatPct } from './IndianNumber'

// Colour intensity scales with magnitude, capped at +/-30% so one wild
// outlier doesn't wash out every other cell in the row.
function heatColor(value) {
  if (value == null) return { background: '#f1f2ef', color: '#8a9086' }
  const capped = Math.max(-30, Math.min(30, value))
  const intensity = Math.abs(capped) / 30
  if (value >= 0) {
    const bg = `rgba(52, 150, 90, ${0.12 + intensity * 0.55})`
    return { background: bg, color: intensity > 0.55 ? '#fff' : '#1c5232' }
  }
  const bg = `rgba(196, 68, 58, ${0.12 + intensity * 0.55})`
  return { background: bg, color: intensity > 0.55 ? '#fff' : '#7a231d' }
}

export default function HeatCell({ value, digits = 2 }) {
  const style = heatColor(value)
  return (
    <td
      className="px-3 py-2 text-right text-sm font-medium tabular-nums whitespace-nowrap rounded"
      style={style}
    >
      {formatPct(value, digits)}
    </td>
  )
}
