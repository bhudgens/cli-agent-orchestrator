import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import { WatchdogAlerts } from './WatchdogAlerts'

afterEach(() => vi.restoreAllMocks())

describe('WatchdogAlerts', () => {
  it('shows actionable watchdog failures', async () => {
    vi.spyOn(api, 'getWatchdog').mockResolvedValue([{ key: 'worker', state: 'mcp_error', reason: 'MCP startup failed', checked_at: 'now' }])
    render(<WatchdogAlerts />)
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('MCP startup failed'))
  })
  it('surfaces unavailable monitoring without hiding the app', async () => {
    vi.spyOn(api, 'getWatchdog').mockRejectedValue(new Error('offline'))
    render(<WatchdogAlerts />)
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('unavailable'))
  })
})
