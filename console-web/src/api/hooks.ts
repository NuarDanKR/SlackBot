/** 화면이 쓰는 데이터 훅.
 *
 * 외부 상태 라이브러리를 쓰지 않습니다. 화면 수가 적고, 필요한 것은
 * "불러오는 중 / 실패 / 결과 / 다시 불러오기" 넷뿐입니다.
 */
import { useCallback, useEffect, useState } from 'react'
import { ApiError, api } from './client'

export interface Resource<T> {
  data: T | null
  loading: boolean
  error: ApiError | null
  reload: () => void
}

export function useResource<T>(path: string | null, deps: unknown[] = []): Resource<T> {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(path !== null)
  const [error, setError] = useState<ApiError | null>(null)
  const [tick, setTick] = useState(0)

  const reload = useCallback(() => setTick((n) => n + 1), [])

  useEffect(() => {
    if (path === null) {
      setLoading(false)
      return
    }
    let alive = true
    setLoading(true)
    setError(null)
    api
      .get<T>(path)
      .then((d) => {
        if (alive) setData(d)
      })
      .catch((e) => {
        // 화면을 벗어난 뒤 도착한 응답으로 상태를 건드리지 않습니다.
        if (alive) setError(e instanceof ApiError ? e : new ApiError(0, String(e)))
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, tick, ...deps])

  return { data, loading, error, reload }
}
