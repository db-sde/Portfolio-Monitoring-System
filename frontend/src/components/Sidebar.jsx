const ICONS = {
  dashboard: <path d="M3 3h8v8H3V3Zm10 0h8v5h-8V3ZM3 13h8v8H3v-8Zm10 3h8v5h-8v-5Z" />,
  snapshot: <path d="M3 17V9m6 8V5m6 12v-6m6 6V3" />,
  'fund-summary': <path d="M4 4h16v4H4zM4 10h7v10H4zm9 0h7v4h-7zm0 6h7v4h-7z" />,
  'portfolio-summary': <path d="M17 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm11 9v-1.5a3.5 3.5 0 0 0-2.5-3.36M15.5 3.13a4 4 0 0 1 0 7.75" />,
  exposure: <path d="M21.21 15.89A10 10 0 1 1 8 2.83M22 12A10 10 0 0 0 12 2v10z" />,
  settings: <path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm9-3a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H19.4a1.65 1.65 0 0 1-1.51-1 1.65 1.65 0 0 1 .33-1.82l.06-.06a2 2 0 1 0-2.83-2.83l-.06.06a1.65 1.65 0 0 1-1.82.33 1.65 1.65 0 0 1-1-1.51V4a2 2 0 1 0-4 0v.09a1.65 1.65 0 0 1-1 1.51 1.65 1.65 0 0 1-1.82-.33l-.06-.06a2 2 0 1 0-2.83 2.83l.06.06a1.65 1.65 0 0 1 .33 1.82 1.65 1.65 0 0 1-1.51 1H4a2 2 0 1 0 0 4h.09a1.65 1.65 0 0 1 1.51 1 1.65 1.65 0 0 1-.33 1.82l-.06.06a2 2 0 1 0 2.83 2.83l.06-.06a1.65 1.65 0 0 1 1.82-.33 1.65 1.65 0 0 1 1 1.51V20a2 2 0 1 0 4 0v-.09a1.65 1.65 0 0 1 1-1.51 1.65 1.65 0 0 1 1.82.33l.06.06a2 2 0 1 0 2.83-2.83l-.06-.06a1.65 1.65 0 0 1-.33-1.82 1.65 1.65 0 0 1 1.51-1H20a2 2 0 1 0 0-4h-.09a1.65 1.65 0 0 1-1.51-1Z" />,
}

const NAV_ITEMS = [
  { key: 'dashboard', label: 'Dashboard' },
  { key: 'snapshot', label: 'Portfolio Snapshot' },
  { key: 'fund-summary', label: 'Fund Summary' },
  { key: 'portfolio-summary', label: 'Portfolio Summary' },
  { key: 'exposure', label: 'Exposure' },
  { key: 'settings', label: 'Settings' },
]

function NavList({ active, onNavigate }) {
  return (
    <nav className="p-3 space-y-0.5">
      {NAV_ITEMS.map((item) => {
        const isActive = active === item.key
        return (
          <button
            key={item.key}
            onClick={() => onNavigate(item.key)}
            className={`w-full flex items-center gap-3 text-left px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
              isActive ? 'bg-accent-tint text-accent-strong' : 'text-ink-2 hover:bg-paper-soft hover:text-ink'
            }`}
          >
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={isActive ? 2.2 : 1.8} strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
              {ICONS[item.key]}
            </svg>
            {item.label}
          </button>
        )
      })}
    </nav>
  )
}

function Brand() {
  return (
    <div className="px-5 py-5 border-b border-line-soft">
      <div className="flex items-center gap-2.5">
        <span className="w-8 h-8 rounded-lg bg-band grid place-items-center text-band-ink font-display font-bold text-sm shrink-0">P</span>
        <span className="font-display font-extrabold text-ink tracking-tight">PortfolioIQ</span>
      </div>
    </div>
  )
}

export default function Sidebar({ active, onNavigate, open, onClose }) {
  return (
    <>
      {/* Desktop */}
      <aside className="hidden md:block w-60 shrink-0 border-r border-line-soft bg-card min-h-screen sticky top-0 h-screen overflow-y-auto">
        <Brand />
        <NavList active={active} onNavigate={onNavigate} />
      </aside>

      {/* Mobile drawer */}
      {open && (
        <div className="md:hidden fixed inset-0 z-40">
          <div className="absolute inset-0 bg-ink/30" onClick={onClose} />
          <aside className="absolute left-0 top-0 h-full w-64 bg-card shadow-xl animate-fade-up">
            <div className="flex items-center justify-between pr-3">
              <Brand />
              <button onClick={onClose} className="p-2 text-ink-3 hover:text-ink" aria-label="Close menu">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 6 6 18M6 6l12 12" /></svg>
              </button>
            </div>
            <NavList active={active} onNavigate={onNavigate} />
          </aside>
        </div>
      )}
    </>
  )
}
