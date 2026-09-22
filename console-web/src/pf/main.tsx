import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '../styles/tokens.css'
import '../styles/app.css'
import PfApp from './App'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <PfApp />
  </StrictMode>,
)
