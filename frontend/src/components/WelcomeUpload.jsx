import { useRef, useState } from 'react'

export default function WelcomeUpload({ onUpload, uploading, error }) {
  const fileInput = useRef(null)
  const [pendingFile, setPendingFile] = useState(null)
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [dragging, setDragging] = useState(false)

  const handleFiles = (file) => {
    if (!file) return
    if (file.name.toLowerCase().endsWith('.json')) {
      onUpload(file, '')
    } else {
      setPendingFile(file)
      setPassword('')
    }
  }

  const submit = () => {
    if (!pendingFile) return
    onUpload(pendingFile, password)
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-6 py-16">
      <div className="max-w-xl w-full text-center animate-fade-up">
        <div className="mx-auto mb-6 w-14 h-14 rounded-2xl bg-band grid place-items-center shadow-sm">
          <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="var(--color-band-ink)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <path d="M3 3v16a2 2 0 0 0 2 2h16" />
            <path d="M7 15.5 11 11l3 3 5-6" />
          </svg>
        </div>

        <span className="inline-flex items-center gap-2 text-xs font-semibold tracking-wide uppercase text-accent-strong mb-3">
          <span className="w-4 h-px bg-accent" /> CAMS · KFintech
        </span>
        <h1 className="font-display text-3xl md:text-[2.4rem] font-extrabold text-ink leading-tight mb-3 text-balance">
          Your whole portfolio,<br />finally in one place.
        </h1>
        <p className="text-ink-2 text-[15px] leading-relaxed mb-8 max-w-md mx-auto">
          Upload your CAS statement and its password. PortfolioIQ parses it, enriches every
          fund with live market data, and gives you XIRR, returns, risk and advisor-by-advisor
          comparisons — kept on your own backend so you can come back to it anytime.
        </p>

        <div className="bg-card border border-line rounded-2xl shadow-sm p-5 text-left">
          <label
            className={`flex flex-col items-center gap-2 rounded-xl border-2 border-dashed p-7 cursor-pointer transition-colors ${
              dragging ? 'border-accent bg-accent-tint' : 'border-line-soft hover:border-ink-3'
            }`}
            onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => { e.preventDefault(); setDragging(false); handleFiles(e.dataTransfer.files?.[0]) }}
          >
            <input
              ref={fileInput}
              type="file"
              accept="application/pdf,.pdf,.json"
              className="hidden"
              onChange={(e) => { handleFiles(e.target.files?.[0]); e.target.value = '' }}
            />
            <span className="w-11 h-11 rounded-xl bg-accent-tint grid place-items-center mb-1">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--color-accent-strong)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 16V4M7 9l5-5 5 5" /><path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" />
              </svg>
            </span>
            {pendingFile ? (
              <span className="font-mono text-sm font-medium text-ink">{pendingFile.name}</span>
            ) : (
              <span className="font-medium text-ink text-sm">
                Drop your CAS PDF here, or <span className="text-accent-strong">browse</span>
              </span>
            )}
            <span className="text-xs text-ink-3">A previously-parsed CAS JSON works too, no password needed</span>
          </label>

          {pendingFile && (
            <div className="mt-4 flex flex-wrap gap-2 items-stretch">
              <div className="relative flex-1 min-w-[160px]">
                <input
                  type={showPassword ? 'text' : 'password'}
                  autoFocus
                  placeholder="PDF password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && submit()}
                  className="w-full rounded-lg border border-line px-3 py-2.5 pr-10 text-sm outline-none focus:border-accent"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((s) => !s)}
                  className="absolute right-1 top-1/2 -translate-y-1/2 w-8 h-8 grid place-items-center text-ink-3 hover:text-ink rounded-md"
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                >
                  {showPassword ? (
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M17.94 17.94A10.94 10.94 0 0 1 12 20c-7 0-11-8-11-8a18.6 18.6 0 0 1 5.06-5.94M9.9 4.24A10.94 10.94 0 0 1 12 4c7 0 11 8 11 8a18.6 18.6 0 0 1-2.16 3.19M1 1l22 22" /></svg>
                  ) : (
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8Z" /><circle cx="12" cy="12" r="3" /></svg>
                  )}
                </button>
              </div>
              <button
                onClick={submit}
                disabled={uploading}
                className="rounded-lg bg-band text-band-ink font-semibold text-sm px-5 py-2.5 disabled:opacity-50 hover:opacity-90 transition-opacity"
              >
                {uploading ? 'Parsing…' : 'Parse statement'}
              </button>
              <button
                onClick={() => setPendingFile(null)}
                className="text-sm text-ink-3 hover:text-ink px-2"
              >
                Cancel
              </button>
            </div>
          )}

          {error && (
            <div className="mt-4 flex items-start gap-2 rounded-lg bg-bad-tint border border-bad/20 text-bad text-sm px-3 py-2.5">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="shrink-0 mt-0.5"><circle cx="12" cy="12" r="10" /><path d="M12 8v5M12 16h.01" /></svg>
              <span>{error}</span>
            </div>
          )}
        </div>

        <div className="mt-7 flex flex-wrap justify-center gap-x-5 gap-y-2 text-xs text-ink-3">
          <span className="inline-flex items-center gap-1.5"><Check /> Parsed on your own backend</span>
          <span className="inline-flex items-center gap-1.5"><Check /> Enriched with live fund data</span>
          <span className="inline-flex items-center gap-1.5"><Check /> Comes back the way you left it</span>
        </div>
      </div>
    </div>
  )
}

function Check() {
  return (
    <span className="w-4 h-4 rounded-full bg-good-tint grid place-items-center">
      <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="var(--color-good)" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6 9 17l-5-5" /></svg>
    </span>
  )
}
