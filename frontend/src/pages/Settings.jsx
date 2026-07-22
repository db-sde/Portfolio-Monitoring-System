import { useEffect, useState } from 'react'
import { api } from '../api'

function Toggle({ checked, onChange }) {
  return (
    <button
      type="button"
      onClick={() => onChange(!checked)}
      className={`relative w-10 h-6 rounded-full transition-colors shrink-0 ${checked ? 'bg-accent' : 'bg-line'}`}
      aria-pressed={checked}
    >
      <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${checked ? 'translate-x-4' : ''}`} />
    </button>
  )
}

function ArnChip({ arn, label, onRelabel, onRemove }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(label || '')

  const commit = () => {
    onRelabel(draft.trim() || arn)
    setEditing(false)
  }

  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-line bg-paper pl-3 pr-1.5 py-1 text-xs">
      <span className="font-mono text-ink-3">{arn}</span>
      <span className="text-ink-3">·</span>
      {editing ? (
        <input
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => e.key === 'Enter' && commit()}
          className="w-24 bg-transparent border-b border-accent outline-none text-ink"
        />
      ) : (
        <button onClick={() => setEditing(true)} className="font-medium text-ink hover:text-accent-strong">
          {label || arn}
        </button>
      )}
      <button onClick={onRemove} className="w-4 h-4 rounded-full grid place-items-center text-ink-3 hover:bg-bad-tint hover:text-bad" aria-label={`Remove ${arn}`}>
        <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3"><path d="M18 6 6 18M6 6l12 12" /></svg>
      </button>
    </span>
  )
}

function InvestorCard({ investor, onRename, onRemove, onAddArn, onRelabelArn, onRemoveArn }) {
  const [newCode, setNewCode] = useState('')
  const [newLabel, setNewLabel] = useState('')
  const arns = investor.arns || []
  const labels = investor.arn_labels || {}

  const submitArn = () => {
    const code = newCode.trim()
    if (!code || arns.includes(code)) return
    onAddArn(code, newLabel.trim() || code)
    setNewCode('')
    setNewLabel('')
  }

  return (
    <div className="rounded-xl border border-line-soft bg-paper/40 p-4">
      <div className="flex items-center justify-between gap-2 mb-3">
        <input
          value={investor.investor_name}
          onChange={(e) => onRename(e.target.value)}
          className="font-display font-semibold text-ink bg-transparent border-b border-transparent hover:border-line focus:border-accent outline-none px-0.5"
        />
        <button onClick={onRemove} className="text-xs font-medium text-ink-3 hover:text-bad">Remove investor</button>
      </div>

      <div className="flex flex-wrap gap-2 mb-3">
        {arns.map((arn) => (
          <ArnChip
            key={arn}
            arn={arn}
            label={labels[arn]}
            onRelabel={(label) => onRelabelArn(arn, label)}
            onRemove={() => onRemoveArn(arn)}
          />
        ))}
        {arns.length === 0 && <span className="text-xs text-ink-3 italic">No ARNs yet — add the codes this investor's folios appear under.</span>}
      </div>

      <div className="flex flex-wrap gap-2">
        <input
          placeholder="ARN code (e.g. ARN-13549)"
          value={newCode}
          onChange={(e) => setNewCode(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && submitArn()}
          className="rounded-lg border border-line px-2.5 py-1.5 text-xs font-mono w-40 outline-none focus:border-accent bg-card"
        />
        <input
          placeholder="Display label (optional)"
          value={newLabel}
          onChange={(e) => setNewLabel(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && submitArn()}
          className="rounded-lg border border-line px-2.5 py-1.5 text-xs w-40 outline-none focus:border-accent bg-card"
        />
        <button onClick={submitArn} className="text-xs font-semibold text-accent-strong hover:text-ink px-2">+ Add ARN</button>
      </div>
    </div>
  )
}

export default function Settings({ onConfigSaved }) {
  const [config, setConfig] = useState(null)
  const [groups, setGroups] = useState([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    api.getConfig().then((c) => {
      setConfig(c)
      setGroups(c.groups || [])
    }).finally(() => setLoading(false))
  }, [])

  const touch = () => setSaved(false)

  const updateGroup = (gi, fn) => {
    setGroups((gs) => gs.map((g, i) => (i === gi ? fn(g) : g)))
    touch()
  }

  const addGroup = () => { setGroups((gs) => [...gs, { group_name: 'New group', investors: [] }]); touch() }
  const removeGroup = (gi) => { setGroups((gs) => gs.filter((_, i) => i !== gi)); touch() }
  const addInvestor = (gi) => updateGroup(gi, (g) => ({ ...g, investors: [...g.investors, { investor_name: 'New investor', arns: [], arn_labels: {} }] }))
  const removeInvestor = (gi, ii) => updateGroup(gi, (g) => ({ ...g, investors: g.investors.filter((_, i) => i !== ii) }))

  const updateInvestor = (gi, ii, fn) => updateGroup(gi, (g) => ({
    ...g,
    investors: g.investors.map((inv, i) => (i === ii ? fn(inv) : inv)),
  }))

  const updatePreference = (key, value) => {
    setConfig((c) => ({ ...c, preferences: { ...c.preferences, [key]: value } }))
    touch()
  }

  const save = async () => {
    setSaving(true)
    const next = { ...config, groups }
    try {
      await api.saveConfig(next)
      setConfig(next)
      setSaved(true)
      onConfigSaved?.()
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <div className="text-sm text-ink-3">Loading…</div>
  if (!config) return null

  return (
    <div className="max-w-3xl space-y-6 animate-fade-up">
      <div className="rounded-xl border border-line-soft bg-card p-5">
        <h2 className="font-display font-semibold text-ink mb-4">Preferences</h2>
        <div className="space-y-4">
          <label className="flex items-center justify-between gap-4">
            <span className="text-sm text-ink-2">Show zero-value (fully redeemed) funds by default</span>
            <Toggle checked={!!config.preferences?.show_zero_value_funds} onChange={(v) => updatePreference('show_zero_value_funds', v)} />
          </label>
          <label className="flex items-center justify-between gap-4">
            <span className="text-sm text-ink-2">Show benchmark comparison</span>
            <Toggle checked={!!config.preferences?.show_benchmark_comparison} onChange={(v) => updatePreference('show_benchmark_comparison', v)} />
          </label>
          <div className="flex items-center justify-between gap-4">
            <span className="text-sm text-ink-2">Primary benchmark</span>
            <input
              type="text"
              value={config.preferences?.primary_benchmark || ''}
              onChange={(e) => updatePreference('primary_benchmark', e.target.value)}
              className="rounded-lg border border-line px-3 py-1.5 text-sm w-48 outline-none focus:border-accent"
            />
          </div>
        </div>
      </div>

      <div className="rounded-xl border border-line-soft bg-card p-5">
        <div className="flex items-center justify-between mb-1">
          <h2 className="font-display font-semibold text-ink">Groups, investors &amp; advisors</h2>
          <button onClick={addGroup} className="text-xs font-semibold text-accent-strong hover:text-ink">+ Add group</button>
        </div>
        <p className="text-xs text-ink-3 mb-4">
          Every ARN code a folio in your CAS shows up under needs to be listed against an
          investor here to be attributed correctly in the Portfolio Summary view.
        </p>

        <div className="space-y-5">
          {groups.map((group, gi) => (
            <div key={gi} className="rounded-xl border border-line p-4">
              <div className="flex items-center justify-between gap-2 mb-3">
                <input
                  value={group.group_name}
                  onChange={(e) => updateGroup(gi, (g) => ({ ...g, group_name: e.target.value }))}
                  className="font-display font-bold text-ink bg-transparent border-b border-transparent hover:border-line focus:border-accent outline-none px-0.5"
                />
                <div className="flex items-center gap-3 shrink-0">
                  <button onClick={() => addInvestor(gi)} className="text-xs font-semibold text-accent-strong hover:text-ink">+ Add investor</button>
                  <button onClick={() => removeGroup(gi)} className="text-xs font-medium text-ink-3 hover:text-bad">Remove group</button>
                </div>
              </div>

              <div className="space-y-3">
                {group.investors.map((investor, ii) => (
                  <InvestorCard
                    key={ii}
                    investor={investor}
                    onRename={(name) => updateInvestor(gi, ii, (inv) => ({ ...inv, investor_name: name }))}
                    onRemove={() => removeInvestor(gi, ii)}
                    onAddArn={(code, label) => updateInvestor(gi, ii, (inv) => ({
                      ...inv,
                      arns: [...(inv.arns || []), code],
                      arn_labels: { ...(inv.arn_labels || {}), [code]: label },
                    }))}
                    onRelabelArn={(code, label) => updateInvestor(gi, ii, (inv) => ({
                      ...inv,
                      arn_labels: { ...(inv.arn_labels || {}), [code]: label },
                    }))}
                    onRemoveArn={(code) => updateInvestor(gi, ii, (inv) => {
                      const { [code]: _drop, ...restLabels } = inv.arn_labels || {}
                      return { ...inv, arns: (inv.arns || []).filter((a) => a !== code), arn_labels: restLabels }
                    })}
                  />
                ))}
                {group.investors.length === 0 && (
                  <div className="text-xs text-ink-3 italic px-1">No investors in this group yet.</div>
                )}
              </div>
            </div>
          ))}
          {groups.length === 0 && (
            <div className="text-sm text-ink-3 text-center py-6">No groups yet — add one to start attributing folios to investors and advisors.</div>
          )}
        </div>
      </div>

      <div className="flex items-center gap-3">
        <button
          onClick={save}
          disabled={saving}
          className="text-sm font-semibold bg-band text-band-ink rounded-lg px-5 py-2.5 disabled:opacity-50 hover:opacity-90"
        >
          {saving ? 'Saving…' : 'Save changes'}
        </button>
        {saved && <span className="text-sm text-good font-medium">Saved.</span>}
      </div>
    </div>
  )
}
