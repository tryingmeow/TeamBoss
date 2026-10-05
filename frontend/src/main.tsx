import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App'

async function boot() {
  // `npm run demo`: answer /api/* from in-memory fixtures. Dead code in production builds.
  if (import.meta.env.MODE === 'demo') {
    const { installDemoApi } = await import('./demo')
    installDemoApi()
  }

  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
}

void boot()
