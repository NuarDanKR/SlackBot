import { useState } from 'react'
import { useResource } from '../api/hooks'
import { Failed, Loading, PageHead, Section, fmt } from '../components/primitives'

type Level = 'info' | 'warning' | 'error'

interface LogResponse {
  level: Level
  entries: { level: Level; message: string }[]
}

const LABEL: Record<Level, string> = { info: 'INFO', warning: 'WARNING', error: 'ERROR' }

export interface ErrorLogContext {
  at: string
  workspace: string
}

export function ServiceLogs({ context }: { context?: ErrorLogContext | null }) {
  const [level, setLevel] = useState<Level>('error')
  const [limit, setLimit] = useState(200)
  const resource = useResource<LogResponse>(`/api/service-logs?level=${level}&limit=${limit}`, [
    level,
    limit,
  ])

  if (resource.loading && !resource.data) return <Loading what="서비스 로그를" />
  if (resource.error && !resource.data) {
    return <Failed what="서비스 로그를" detail={resource.error.message} onRetry={resource.reload} />
  }

  return (
    <>
      <PageHead
        crumb="운영 관리"
        title="서비스 로그"
        note="TYBot 서비스의 최근 로그를 레벨별로 조회합니다. 오류는 traceback 전체를 함께 표시하고, 시크릿 패턴은 서버에서 마스킹합니다."
        aside={
          <button className="btn btn-sm" type="button" onClick={resource.reload}>
            새로고침
          </button>
        }
      />
      {context && (
        <div className="notice watch">
          <div className="notice-kind">질문 오류</div>
          <div>
            <div className="notice-title">
              {context.workspace} 워크스페이스 · {fmt.dayClock(context.at)} 질문 기록에서
              이동했습니다
            </div>
            <div className="notice-detail">
              아래 ERROR 로그에서 같은 시각대의 traceback을 확인하세요.
            </div>
          </div>
        </div>
      )}
      <Section
        title="최근 기록"
        note={`${resource.data?.entries.length ?? 0}건`}
        aside={
          <div className="form-row" style={{ marginTop: 0 }}>
            {(['info', 'warning', 'error'] as Level[]).map((value) => (
              <button
                key={value}
                className={`btn btn-sm ${level === value ? 'btn-primary' : ''}`}
                type="button"
                onClick={() => setLevel(value)}
              >
                {LABEL[value]}
              </button>
            ))}
            <select
              className="input"
              style={{ width: 92 }}
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
            >
              <option value={100}>100건</option>
              <option value={200}>200건</option>
              <option value={500}>500건</option>
            </select>
          </div>
        }
      >
        <div className="service-log" role="log" aria-live="polite">
          {(resource.data?.entries ?? []).map((entry, index) => (
            <div className={`service-log-line ${entry.level}`} key={`${index}-${entry.message}`}>
              <span>{LABEL[entry.level]}</span>
              <code>{entry.message}</code>
            </div>
          ))}
          {!resource.data?.entries.length && <div className="empty-note">해당 레벨의 로그가 없습니다.</div>}
        </div>
      </Section>
    </>
  )
}
