import { useCallback, useEffect, useState } from 'react'

export interface HashLocation {
  path: string
  query: URLSearchParams
}

function readHash(): HashLocation {
  const raw = window.location.hash.replace(/^#/, '') || '/collect'
  const [path, search = ''] = raw.split('?', 2)
  return { path: path.startsWith('/') ? path : `/${path}`, query: new URLSearchParams(search) }
}

export function useHashNavigation() {
  const [location, setLocation] = useState<HashLocation>(readHash)
  useEffect(() => {
    const changed = () => setLocation(readHash())
    window.addEventListener('hashchange', changed)
    return () => window.removeEventListener('hashchange', changed)
  }, [])
  const navigate = useCallback((target: string) => {
    const value = target.startsWith('/') ? target : `/${target}`
    if (`#${value}` === window.location.hash) setLocation(readHash())
    else window.location.hash = value
  }, [])
  return { location, navigate }
}

export function withQuery(path: string, values: Record<string, string | null | undefined>) {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(values)) if (value) query.set(key, value)
  const suffix = query.toString()
  return suffix ? `${path}?${suffix}` : path
}
