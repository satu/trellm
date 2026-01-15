import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import App from './App'

describe('App', () => {
  it('renders the TreLLM heading', () => {
    render(<App />)
    expect(screen.getByRole('heading', { name: /TreLLM/i })).toBeInTheDocument()
  })

  it('renders the welcome message', () => {
    render(<App />)
    expect(screen.getByText(/Welcome to TreLLM/i)).toBeInTheDocument()
  })
})
