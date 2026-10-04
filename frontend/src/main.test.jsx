import { act, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'

vi.mock('./App.jsx', () => ({ default: () => <div>Portfolio workspace</div> }))

it('opens the app directly without requesting a session or showing a password form', async () => {
  document.body.innerHTML = '<div id="root"></div>'
  const fetch = vi.fn(() => { throw new Error('App entry must not request a session') })
  vi.stubGlobal('fetch', fetch)
  try {
    await act(async () => { await import('./main.jsx') })
    expect(screen.getByText('Portfolio workspace')).toBeTruthy()
    expect(screen.queryByLabelText('Owner password')).toBeNull()
    expect(fetch).not.toHaveBeenCalled()
  } finally {
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  }
})
