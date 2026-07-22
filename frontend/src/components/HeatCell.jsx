import { formatPct } from './IndianNumber'

// Colour intensity scales with magnitude, capped at +/-30% so one wild
// outlier doesn't wash out every other cell in the row.
function heatColor(value) {
  if (value == null) return { background: '#ece9e0', color: '#8a8e9c' }
  const capped = Math.max(-30, Math.min(30, value))
  const intensity = Math.abs(capped) / 30
  if (value >= 0) {
    const bg = `rgba(31, 122, 77, ${0.12 + intensity * 0.55})`
    return { background: bg, color: intensity > 0.55 ? '#fff' : '#164f32' }
  }
  const bg = `rgba(178, 59, 46, ${0.12 + intensity * 0.55})`
  return { background: bg, color: intensity > 0.55 ? '#fff' : '#7a2a20' }
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
