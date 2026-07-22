import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Settings() {
  const [config, setConfig] = useState(null)
  const [groupsJson, setGroupsJson] = useState('')
  const [jsonError, setJsonError] = useState(null)
  const [saved, setSaved] = useState(false)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.getConfig().then((c) => {
      setConfig(c)
      setGroupsJson(JSON.stringify(c.groups, null, 2))
    }).finally(() => setLoading(false))
  }, [])

  const updatePreference = (key, value) => {
    setConfig((c) => ({ ...c, preferences: { ...c.preferences, [key]: value } }))
    setSaved(false)
  }

  const save = async () => {
    let groups
    try {
      groups = JSON.parse(groupsJson)
      setJsonError(null)
    } catch {
      setJsonError('That’s not valid JSON — fix the syntax before saving.')
      return
    }
    const next = { ...config, groups }
    await api.saveConfig(next)
    setConfig(next)
    setSaved(true)
  }

  if (loading) return <div className="text-sm text-gray-400">Loading…</div>
  if (!config) return null

  return (
    <div className="max-w-3xl space-y-6">
      <div className="rounded-xl border border-gray-200 bg-white p-5">
        <h2 className="text-sm font-semibold text-gray-800 mb-4">Preferences</h2>
        <div className="space-y-4">
          <label className="flex items-center justify-between">
            <span className="text-sm text-gray-700">Show zero-value (fully redeemed) funds by default</span>
            <input
              type="checkbox"
              checked={!!config.preferences?.show_zero_value_funds}
              onChange={(e) => updatePreference('show_zero_value_funds', e.target.checked)}
              className="h-4 w-4"
            />
          </label>
          <label className="flex items-center justify-between">
            <span className="text-sm text-gray-700">Show benchmark comparison</span>
            <input
              type="checkbox"
              checked={!!config.preferences?.show_benchmark_comparison}
              onChange={(e) => updatePreference('show_benchmark_comparison', e.target.checked)}
              className="h-4 w-4"
            />
          </label>
          <div className="flex items-center justify-between">
            <span className="text-sm text-gray-700">Primary benchmark</span>
            <input
              type="text"
              value={config.preferences?.primary_benchmark || ''}
              onChange={(e) => updatePreference('primary_benchmark', e.target.value)}
              className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm w-48"
            />
          </div>
        </div>
      </div>

      <div className="rounded-xl border border-gray-200 bg-white p-5">
        <h2 className="text-sm font-semibold text-gray-800 mb-2">Groups, investors &amp; ARN labels</h2>
        <p className="text-xs text-gray-500 mb-3">
          Raw JSON editor — this is the same shape as <code className="font-mono">config.json</code>&apos;s{' '}
          <code className="font-mono">groups</code> array. Every ARN code your CAS shows up under needs to be
          listed here to be attributed to a group/investor in the Portfolio Summary view.
        </p>
        <textarea
          value={groupsJson}
          onChange={(e) => { setGroupsJson(e.target.value); setSaved(false) }}
          rows={16}
          spellCheck={false}
          className="w-full rounded-lg border border-gray-200 px-3 py-2 text-xs font-mono bg-gray-50"
        />
        {jsonError && <div className="text-xs text-red-600 mt-2">{jsonError}</div>}
      </div>

      <div className="flex items-center gap-3">
        <button onClick={save} className="text-sm font-medium bg-emerald-700 hover:bg-emerald-800 text-white rounded-lg px-4 py-2">
          Save config
        </button>
        {saved && <span className="text-sm text-emerald-700">Saved.</span>}
      </div>
    </div>
  )
}
