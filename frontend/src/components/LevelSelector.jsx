// Group / Investor / Advisor(ARN) toggle, driven off config.json's own
// groups -> investors -> arns tree so it never shows a choice the
// portfolio can't actually be filtered by.
export default function LevelSelector({ config, level, groupName, investorName, arn, onChange }) {
  const groups = config?.groups || []
  const investors = groups.flatMap((g) => g.investors.map((i) => ({ ...i, group_name: g.group_name })))
  const arns = investors.flatMap((i) =>
    (i.arns || []).map((a) => ({ arn: a, label: i.arn_labels?.[a] || a, investor_name: i.investor_name }))
  )

  const setLevel = (newLevel) => {
    if (newLevel === 'all') {
      onChange({ level: null, groupName: null, investorName: null, arn: null })
    } else {
      onChange({ level: newLevel, groupName: null, investorName: null, arn: null })
    }
  }

  const tabs = [
    { key: 'all', label: 'All' },
    { key: 'group', label: 'Group' },
    { key: 'investor', label: 'Investor' },
    { key: 'arn', label: 'Advisor' },
  ]
  const active = level || 'all'

  return (
    <div className="flex flex-wrap items-center gap-3">
      <div className="inline-flex rounded-lg border border-line bg-card p-0.5">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setLevel(t.key)}
            className={`px-3.5 py-1.5 text-sm font-medium rounded-md transition-colors ${
              active === t.key ? 'bg-band text-band-ink' : 'text-ink-2 hover:bg-paper-soft'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {level === 'group' && (
        <select
          className="rounded-lg border border-line bg-card px-3 py-1.5 text-sm outline-none focus:border-accent"
          value={groupName || ''}
          onChange={(e) => onChange({ level, groupName: e.target.value || null, investorName: null, arn: null })}
        >
          <option value="">Select a group…</option>
          {groups.map((g) => (
            <option key={g.group_name} value={g.group_name}>{g.group_name}</option>
          ))}
        </select>
      )}

      {level === 'investor' && (
        <select
          className="rounded-lg border border-line bg-card px-3 py-1.5 text-sm outline-none focus:border-accent"
          value={investorName || ''}
          onChange={(e) => onChange({ level, groupName: null, investorName: e.target.value || null, arn: null })}
        >
          <option value="">Select an investor…</option>
          {investors.map((i) => (
            <option key={i.investor_name} value={i.investor_name}>{i.investor_name}</option>
          ))}
        </select>
      )}

      {level === 'arn' && (
        <select
          className="rounded-lg border border-line bg-card px-3 py-1.5 text-sm outline-none focus:border-accent"
          value={arn || ''}
          onChange={(e) => onChange({ level, groupName: null, investorName: null, arn: e.target.value || null })}
        >
          <option value="">Select an advisor…</option>
          {arns.map((a) => (
            <option key={a.arn} value={a.arn}>{a.label}</option>
          ))}
        </select>
      )}
    </div>
  )
}
