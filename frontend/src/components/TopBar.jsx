import { useRef, useState } from 'react'

export default function TopBar({ investorName, statementPeriod, lastEnriched, enrichStatus, onUpload, uploading, onMenuClick }) {
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
    <header className="border-b border-line-soft bg-card px-4 md:px-6 py-3">
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-3 min-w-0">
          <button
            onClick={onMenuClick}
            className="md:hidden shrink-0 p-2 -ml-2 text-ink-2 hover:text-ink"
            aria-label="Open menu"
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M4 6h16M4 12h16M4 18h16" /></svg>
          </button>
          <div className="min-w-0">
            <div className="font-display font-semibold text-ink truncate">{investorName}</div>
            <div className="text-xs text-ink-3 truncate">
              {statementPeriod ? `${statementPeriod.from} – ${statementPeriod.to}` : ''}
              {lastEnriched && ` · enriched ${new Date(lastEnriched).toLocaleString()}`}
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2.5 shrink-0">
          {enrichStatus && enrichStatus.pending > 0 && (
            <span className="hidden sm:inline-flex items-center gap-1.5 text-xs font-medium text-warn bg-warn-tint rounded-full px-2.5 py-1">
              <svg width="12" height="12" viewBox="0 0 24 24" className="animate-spin"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="3" strokeDasharray="42" strokeDashoffset="14" strokeLinecap="round" /></svg>
              Enriching {enrichStatus.total_schemes - enrichStatus.pending}/{enrichStatus.total_schemes}
            </span>
          )}
          <input ref={fileInput} type="file" accept="application/pdf,.pdf,.json" className="hidden" onChange={handleFile} />
          <button
            onClick={handlePick}
            disabled={uploading || !!pendingFile}
            className="text-sm font-semibold text-ink-2 hover:text-ink border border-line rounded-lg px-3.5 py-2 disabled:opacity-50 transition-colors"
          >
            {uploading ? 'Parsing…' : 'New statement'}
          </button>
        </div>
      </div>

      {pendingFile && (
        <div className="mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-accent/30 bg-accent-tint px-3 py-2.5">
          <span className="text-sm text-ink font-mono">{pendingFile.name}</span>
          <input
            type="password"
            autoFocus
            placeholder="PDF password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && submitPending()}
            className="rounded-lg border border-line px-3 py-1.5 text-sm flex-1 min-w-[160px] outline-none focus:border-accent bg-card"
          />
          <button onClick={submitPending} className="text-sm font-semibold bg-band text-band-ink rounded-lg px-3.5 py-1.5 hover:opacity-90">
            Parse
          </button>
          <button onClick={cancelPending} className="text-sm font-medium text-ink-3 hover:text-ink px-2 py-1.5">
            Cancel
          </button>
        </div>
      )}
    </header>
  )
}
