import { useRef } from 'react'

export default function TopBar({ investorName, statementPeriod, lastEnriched, enrichStatus, onUpload, uploading }) {
  const fileInput = useRef(null)

  const handlePick = () => fileInput.current?.click()
  const handleFile = (e) => {
    const file = e.target.files?.[0]
    if (file) onUpload(file)
    e.target.value = ''
  }

  return (
    <header className="border-b border-gray-200 bg-white px-6 py-3 flex items-center justify-between gap-4">
      <div>
        {investorName ? (
          <>
            <div className="font-semibold text-gray-900">{investorName}</div>
            <div className="text-xs text-gray-500">
              {statementPeriod ? `${statementPeriod.from} – ${statementPeriod.to}` : ''}
              {lastEnriched && ` · enriched ${new Date(lastEnriched).toLocaleString()}`}
            </div>
          </>
        ) : (
          <div className="text-sm text-gray-500">No CAS statement uploaded yet</div>
        )}
      </div>

      <div className="flex items-center gap-3">
        {enrichStatus && enrichStatus.pending > 0 && (
          <span className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-full px-2.5 py-1">
            Enriching {enrichStatus.total_schemes - enrichStatus.pending}/{enrichStatus.total_schemes}…
          </span>
        )}
        <input ref={fileInput} type="file" accept="application/json" className="hidden" onChange={handleFile} />
        <button
          onClick={handlePick}
          disabled={uploading}
          className="text-sm font-medium bg-emerald-700 hover:bg-emerald-800 disabled:opacity-50 text-white rounded-lg px-4 py-2"
        >
          {uploading ? 'Uploading…' : 'Upload CAS JSON'}
        </button>
      </div>
    </header>
  )
}
