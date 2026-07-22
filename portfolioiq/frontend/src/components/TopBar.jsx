import { useRef, useState } from 'react'

export default function TopBar({ investorName, statementPeriod, lastEnriched, enrichStatus, onUpload, uploading }) {
  const fileInput = useRef(null)
  const [pendingFile, setPendingFile] = useState(null)
  const [password, setPassword] = useState('')

  const handlePick = () => fileInput.current?.click()

  const handleFile = (e) => {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    if (file.name.toLowerCase().endsWith('.json')) {
      // Pre-parsed JSON needs no password — go straight to upload.
      onUpload(file, '')
    } else {
      // Real CAS PDFs are always password-protected — hold the file and
      // ask for the password before submitting.
      setPendingFile(file)
      setPassword('')
    }
  }

  const submitPending = () => {
    if (!pendingFile) return
    onUpload(pendingFile, password)
    setPendingFile(null)
    setPassword('')
  }

  const cancelPending = () => {
    setPendingFile(null)
    setPassword('')
  }

  return (
    <header className="border-b border-gray-200 bg-white px-6 py-3">
      <div className="flex items-center justify-between gap-4">
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
            <div className="text-sm text-gray-500">Upload your CAS statement to get started</div>
          )}
        </div>

        <div className="flex items-center gap-3">
          {enrichStatus && enrichStatus.pending > 0 && (
            <span className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-full px-2.5 py-1">
              Enriching {enrichStatus.total_schemes - enrichStatus.pending}/{enrichStatus.total_schemes}…
            </span>
          )}
          <input ref={fileInput} type="file" accept="application/pdf,.pdf,.json" className="hidden" onChange={handleFile} />
          <button
            onClick={handlePick}
            disabled={uploading || !!pendingFile}
            className="text-sm font-medium bg-emerald-700 hover:bg-emerald-800 disabled:opacity-50 text-white rounded-lg px-4 py-2"
          >
            {uploading ? 'Parsing…' : 'Upload CAS PDF'}
          </button>
        </div>
      </div>

      {pendingFile && (
        <div className="mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2">
          <span className="text-sm text-gray-700 font-mono">{pendingFile.name}</span>
          <input
            type="password"
            autoFocus
            placeholder="PDF password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && submitPending()}
            className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm flex-1 min-w-[160px]"
          />
          <button onClick={submitPending} className="text-sm font-medium bg-emerald-700 hover:bg-emerald-800 text-white rounded-lg px-3 py-1.5">
            Parse
          </button>
          <button onClick={cancelPending} className="text-sm font-medium text-gray-500 hover:text-gray-800 px-2 py-1.5">
            Cancel
          </button>
        </div>
      )}
    </header>
  )
}
