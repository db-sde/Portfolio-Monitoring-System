import { useEffect, useState } from 'react'
import { api } from '../api'
import { formatIndian, formatPct } from '../components/IndianNumber'
import SkeletonTable from '../components/SkeletonTable'
import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip, Legend } from 'recharts'

const CAP_COLORS = ['#0f6e3f', '#4fa876', '#a8d5b8', '#d8ddd3']

function ExposureTable({ title, rows, nameKey }) {
  return (
    <div className="rounded-xl border border-gray-200 bg-white overflow-hidden">
      <div className="text-sm font-semibold text-gray-800 px-4 py-3 border-b border-gray-100">{title}</div>
      <table className="w-full text-sm">
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-t border-gray-100 first:border-0">
              <td className="px-4 py-2 font-medium text-gray-900">{r[nameKey]}</td>
              <td className="px-4 py-2 text-right tabular-nums">{formatIndian(r.current_value)}</td>
              <td className="px-4 py-2 text-right tabular-nums text-gray-500 w-24">{formatPct(r.pct_of_portfolio, 1)}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr><td colSpan={3} className="px-4 py-6 text-center text-gray-400">Nothing to show.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  )
}

export default function Exposure({ refreshTick }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    setLoading(true)
    setError(null)
    api.getExposure().then(setData).catch((err) => setError(err.message)).finally(() => setLoading(false))
  }, [refreshTick])

  if (error) return <div className="text-sm text-red-600">{error}</div>
  if (loading) return <SkeletonTable rows={6} cols={3} />
  if (!data) return null

  const cap = data.cap_allocation || {}
  const pieData = [
    { name: 'Large cap', value: cap.largecap_pct },
    { name: 'Mid cap', value: cap.midcap_pct },
    { name: 'Small cap', value: cap.smallcap_pct },
    { name: 'Other/unclassified', value: cap.other_pct },
  ].filter((d) => d.value != null && d.value > 0)

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <ExposureTable title="Top AMCs" rows={data.top_amcs || []} nameKey="amc_name" />
      <ExposureTable title="Top funds" rows={data.top_funds || []} nameKey="scheme_name" />
      <div className="rounded-xl border border-gray-200 bg-white p-4 lg:col-span-2">
        <div className="text-sm font-semibold text-gray-800 mb-3">Cap allocation across held equity funds</div>
        {pieData.length ? (
          <ResponsiveContainer width="100%" height={260}>
            <PieChart>
              <Pie data={pieData} dataKey="value" nameKey="name" innerRadius={60} outerRadius={100} paddingAngle={2}>
                {pieData.map((_, i) => <Cell key={i} fill={CAP_COLORS[i % CAP_COLORS.length]} />)}
              </Pie>
              <Legend />
              <Tooltip formatter={(v) => `${v.toFixed(1)}%`} />
            </PieChart>
          </ResponsiveContainer>
        ) : (
          <div className="text-sm text-gray-400 py-10 text-center">
            No cap-allocation data available (needs mfdata.in enrichment on the held funds).
          </div>
        )}
      </div>
    </div>
  )
}
