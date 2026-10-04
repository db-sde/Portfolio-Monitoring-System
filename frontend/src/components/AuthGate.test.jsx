import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import AuthGate from './AuthGate'
import { api } from '../api'

vi.mock('../api', () => ({ api: { getSession: vi.fn() }, invalidateQueries: vi.fn() }))
afterEach(cleanup)
const Workspace = ({ authRequired }) => <div>Workspace {authRequired ? 'protected' : 'local'}</div>

it('opens the local workspace directly without a password form', async () => {
  api.getSession.mockResolvedValue({ authenticated: true, password_required: false })
  render(<AuthGate><Workspace /></AuthGate>)
  expect(await screen.findByText('Workspace local')).toBeTruthy()
  expect(screen.queryByLabelText('Owner password')).toBeNull()
})

it('still requires sign-in for a protected hosted workspace', async () => {
  api.getSession.mockResolvedValue({ authenticated: false, password_required: true })
  render(<AuthGate><Workspace /></AuthGate>)
  expect(await screen.findByLabelText('Owner password')).toBeTruthy()
  expect(screen.queryByText('Workspace protected')).toBeNull()
})
