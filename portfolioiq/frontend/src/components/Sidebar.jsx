const NAV_ITEMS = [
  { key: 'dashboard', label: 'Dashboard' },
  { key: 'snapshot', label: 'Portfolio Snapshot' },
  { key: 'fund-summary', label: 'Fund Summary' },
  { key: 'portfolio-summary', label: 'Portfolio Summary' },
  { key: 'exposure', label: 'Exposure' },
  { key: 'settings', label: 'Settings' },
]

export default function Sidebar({ active, onNavigate }) {
  return (
    <aside className="w-56 shrink-0 border-r border-gray-200 bg-white min-h-screen">
      <div className="px-5 py-5 border-b border-gray-100">
        <div className="flex items-center gap-2">
          <span className="w-8 h-8 rounded-lg bg-emerald-700 grid place-items-center text-white font-bold text-sm">P</span>
          <span className="font-semibold text-gray-900 tracking-tight">PortfolioIQ</span>
        </div>
      </div>
      <nav className="p-3 space-y-1">
        {NAV_ITEMS.map((item) => (
          <button
            key={item.key}
            onClick={() => onNavigate(item.key)}
            className={`w-full text-left px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
              active === item.key
                ? 'bg-emerald-50 text-emerald-800'
                : 'text-gray-600 hover:bg-gray-50 hover:text-gray-900'
            }`}
          >
            {item.label}
          </button>
        ))}
      </nav>
    </aside>
  )
}
