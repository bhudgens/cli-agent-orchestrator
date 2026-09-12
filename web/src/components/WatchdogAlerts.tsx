import { useEffect, useState } from 'react'
import { api } from '../api'

export interface WatchdogCheck {
  key: string
  state: string
  reason?: string
  checked_at: string
}

export function WatchdogAlerts() {
  const [checks, setChecks] = useState<WatchdogCheck[]>([])
  const [error, setError] = useState(false)
  useEffect(() => {
    let active = true
    const refresh = async () => {
      try {
        const next = await api.getWatchdog()
        if (active) { setChecks(Array.isArray(next) ? next : []); setError(false) }
      } catch { if (active) setError(true) }
    }
    void refresh()
    const timer = setInterval(() => { void refresh() }, 30000)
    return () => { active = false; clearInterval(timer) }
  }, [])
  const alerts = checks.filter(check => ['unmanaged', 'missing', 'error', 'mcp_error', 'delivery_failed'].includes(check.state))
  if (!error && alerts.length === 0) return null
  return <aside role="status" className="mx-4 mt-3 rounded border border-amber-700 bg-amber-950/30 p-3 text-sm text-amber-200">
    <p>{error ? 'Watchdog status unavailable.' : `Watchdog: ${alerts.length} item(s) need attention.`}</p>
    {alerts.slice(0, 10).map(check => <p key={check.key}>
      {check.key}: {check.reason || check.state} <span className="text-xs">({check.checked_at})</span>
    </p>)}
  </aside>
}
