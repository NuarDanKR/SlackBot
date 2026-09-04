import { useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import type { ConsoleUser, HealthReport, HealthLevel } from '../types'
import { Chip, Failed, Loading, Metric, PageHead, Section, fmt } from '../components/primitives'

/**
 * 헬스 체크 — "돌고는 있는데 제 일을 못 하는" 상태를 드러냅니다.
 *
 * 프로세스가 살아 있는지는 서버가 따로 답합니다. 이 화면은 그 다음을 봅니다.
 * 수집이 비어 가거나, 답이 근거를 못 찾거나, 명령이 Slack 에 등록되지 않은 것은
 * 오류 로그를 남기지 않습니다. 숫자로 드러내지 않으면 아무도 모릅니다.
 */

const LEVEL_LABEL: Record<HealthLevel, string> = {
  ok: '정상',
  warn: '확인 필요',
  bad: '조치 필요',
  unknown: '판단 보류',
}

const LEVEL_TONE: Record<HealthLevel, 'ok' | 'watch' | 'bad' | 'plain'> = {
  ok: 'ok',
  warn: 'watch',
  bad: 'bad',
  unknown: 'plain',
}

type HealthTab = 'bot' | 'archive' | 'answers' | 'commands' | 'feedback'

const HEALTH_TABS: { id: HealthTab; label: string }[] = [
  { id: 'bot', label: '봇 연결' },
  { id: 'archive', label: '아카이브' },
  { id: 'answers', label: '답변 품질' },
  { id: 'commands', label: '슬래시 명령' },
  { id: 'feedback', label: '피드백' },
]

function levelChip(level: HealthLevel) {
  return <Chip tone={LEVEL_TONE[level]}>{LEVEL_LABEL[level]}</Chip>
}

/** 비율을 퍼센트로. 값이 없으면 판단하지 않았다는 뜻이라 '—' 로 둡니다. */
function pct(value: number | null | undefined) {
  return value == null ? '—' : `${Math.round(value * 100)}%`
}

function Problems({ items }: { items: string[] }) {
  if (!items.length) return null
  return (
    <ul className="health-problems">
      {items.map((p) => (
        <li key={p}>{p}</li>
      ))}
    </ul>
  )
}

const KIND_LABEL: Record<string, string> = {
  positive: '👍 정확했다', negative: '👎 틀렸다',
  missing: '🔍 근거 못 찾음', correction: '정정 제보',
}

export function HealthCheck({ user }: { user: ConsoleUser }) {
  const [tab, setTab] = useState<HealthTab>('bot')
  const [busy, setBusy] = useState<string | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  // 서버가 이미 권한 범위로 좁혀서 내려 줍니다. 화면에서 다시 거르지 않습니다.
  const res = useResource<HealthReport>('/api/health-report')

  if (res.loading) return <Loading what="헬스 체크를" />
  if (res.error || !res.data)
    return <Failed what="헬스 체크를" detail={res.error?.message ?? '데이터가 비어 있습니다'} onRetry={res.reload} />

  // 신고를 처리했다고 표시합니다. 신고를 지우거나 고치지 않고 한 줄을 더 쌓습니다.
  async function markHandled(id: string) {
    const note = window.prompt('무엇을 고쳤는지 적어 주세요(선택).') ?? ''
    setBusy(id); setFailed(null)
    try {
      await api.put(`/api/health-report/feedback/${id}/handled`, { note })
      res.reload()
    } catch (caught) {
      setFailed(caught instanceof ApiError ? caught.message : String(caught))
    } finally { setBusy(null) }
  }

  const r = res.data
  const isAdmin = user.role === 'admin'
  const { bot, archive, answers, commands, feedback } = r.sections

  return (
    <>
      <PageHead
        crumb="운영 관리"
        title="헬스 체크"
        note={
          `최근 ${r.days}일 기준입니다. ` +
          '오류 없이 조용히 잘못 도는 상태를 찾는 것이 목적입니다. ' +
          '자료가 부족한 항목은 정상으로 칠하지 않고 "판단 보류" 로 둡니다.'
        }
        aside={levelChip(r.level)}
      />

      {r.problems.length > 0 && (
        <Section
          title="지금 확인할 것"
          lead="아래 항목은 사용자 화면에 오류로 나타나지 않습니다. 그래서 여기에 모아 둡니다."
        >
          <ul className="health-problems">
            {r.problems.map((p, i) => (
              <li key={`${p.section}-${i}`}>
                <strong>{p.section}</strong> · {p.message}
              </li>
            ))}
          </ul>
        </Section>
      )}

      <div className="subtabs" role="tablist" aria-label="헬스 체크 항목">
        {HEALTH_TABS.map((item) => (
          <button
            className={`subtab ${tab === item.id ? 'is-active' : ''}`}
            type="button"
            role="tab"
            aria-selected={tab === item.id}
            key={item.id}
            onClick={() => setTab(item.id)}
          >
            {item.label}
            {levelChip(r.sections[item.id].level)}
          </button>
        ))}
      </div>

      {tab === 'bot' && (
        <Section
          title="봇 연결"
          aside={levelChip(bot.level)}
          lead="Slack 에 붙어 있는지, 원문을 저장할 수 있는지, 초대되지 않아 수집에서 빠진 채널이 있는지 봅니다."
        >
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>워크스페이스</th>
                <th>상태</th>
                <th>연결</th>
                <th>문제</th>
              </tr>
            </thead>
            <tbody>
              {bot.workspaces.map((w) => (
                <tr key={w.workspace}>
                  <td>{w.label}</td>
                  <td>{levelChip(w.level)}</td>
                  <td>{w.connected === null ? '알 수 없음' : w.connected ? '연결됨' : '끊김'}</td>
                  <td>{w.problems.length ? <Problems items={w.problems} /> : '—'}</td>
                </tr>
              ))}
              {!bot.workspaces.length && (
                <tr>
                  <td colSpan={4}>등록된 워크스페이스가 없습니다.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        </Section>
      )}

      {tab === 'archive' && (
        <Section
          title="아카이브"
          aside={levelChip(archive.level)}
          lead="스키마가 깨진 문서는 검색에서 조용히 빠집니다. 답이 '없다' 로 바뀌는 흔한 원인입니다."
        >
        <div className="metrics">
          <Metric k="수집 문서" v={archive.documents.toLocaleString()} unit="건" />
          <Metric k="깨진 문서" v={archive.brokenDocuments.toLocaleString()} unit="건" />
          <Metric k="수집 밀린 워크스페이스" v={archive.staleWorkspaces.toLocaleString()} unit="개" />
        </div>
        <Problems items={archive.problems} />
        </Section>
      )}

      {tab === 'answers' && (
        <Section
          title="답변 품질"
          aside={levelChip(answers.level)}
          lead="근거를 찾아 답했는지를 봅니다. 답이 나가는 것과 쓸모 있는 것은 다릅니다."
        >
        <div className="metrics">
          <Metric k="질문" v={answers.questions.toLocaleString()} unit="건" />
          <Metric k="근거 찾음" v={pct(answers.groundedRate)} />
          <Metric k="근거 못 찾음" v={(answers.noHits ?? 0).toLocaleString()} unit="건" />
          <Metric k="오류" v={(answers.errors ?? 0).toLocaleString()} unit="건" />
        </div>
        {answers.note && <p className="section-lead">{answers.note}</p>}
        <Problems items={answers.problems} />
        {!!answers.topErrors?.length && (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>실패한 예외</th><th>건수</th></tr></thead>
              <tbody>
                {answers.topErrors.map((e) => (
                  <tr key={e.kind}>
                    <td className="mono">{e.kind}</td>
                    <td>{e.count.toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {!!answers.topReasons?.length && (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>답변 사유</th>
                  <th>건수</th>
                </tr>
              </thead>
              <tbody>
                {answers.topReasons.map((t) => (
                  <tr key={t.reason}>
                    <td>{t.reason}</td>
                    <td>{t.count.toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        </Section>
      )}

      {tab === 'commands' && (
        <Section
          title="슬래시 명령"
          aside={levelChip(commands.level)}
          lead={
            '코드와 매니페스트가 어긋나면 오류가 나지 않습니다. ' +
            '매니페스트에 없으면 Slack 이 명령을 모르고, 코드에 없으면 봇이 응답하지 않습니다.'
          }
        >
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>명령</th>
                <th>코드</th>
                <th>매니페스트</th>
              </tr>
            </thead>
            <tbody>
              {commands.commands.map((c) => (
                <tr key={c.name}>
                  <td>{c.name}</td>
                  <td>{c.inCode ? '있음' : <Chip tone="bad">없음</Chip>}</td>
                  <td>{c.inManifest ? '있음' : <Chip tone="bad">없음</Chip>}</td>
                </tr>
              ))}
              {!commands.commands.length && (
                <tr>
                  <td colSpan={3}>{commands.note ?? '명령을 찾지 못했습니다.'}</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <Problems items={commands.problems} />
        </Section>
      )}

      {tab === 'feedback' && (
        <>
          <Section
        title="답변 만족도"
        aside={levelChip(feedback.level)}
        lead="👍 / 👎 반응과 /피드백 명령을 합쳐 셉니다. 눌렀다 취소한 것은 빼고 셉니다."
      >
        <div className="metrics">
          <Metric k="만족도" v={pct(feedback.satisfaction)} />
          <Metric k="좋아요" v={feedback.positive.toLocaleString()} unit="건" />
          <Metric k="틀렸다" v={feedback.negative.toLocaleString()} unit="건" />
          <Metric k="근거 못 찾음" v={feedback.missing.toLocaleString()} unit="건" />
          <Metric k="정정 제보" v={feedback.corrections.toLocaleString()} unit="건" />
          <Metric k="미처리 신고" v={feedback.openCorrections.toLocaleString()} unit="건" />
        </div>
        {feedback.note && <p className="section-lead">{feedback.note}</p>}
        <Problems items={feedback.problems} />
          </Section>

          {failed && <div className="notice bad"><div className="notice-kind">처리 실패</div><div>{failed}</div></div>}

          <Section
            title="접수된 신고"
            lead={
              isAdmin
                ? "처리하지 않은 것이 위로 옵니다. [처리 표시]는 신고를 지우지 않고 처리 기록을 한 줄 더 남깁니다."
                : "신고 본문은 관리자만 볼 수 있습니다. 업무 내용이 들어 있습니다."
            }
          >
            {feedback.items.length ? (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>접수</th><th>워크스페이스</th><th>보낸 사람</th>
                      <th>종류</th><th>내용</th><th>처리</th>
                    </tr>
                  </thead>
                  <tbody>
                    {feedback.items.map((item) => (
                      <tr key={item.id}>
                        <td className="nowrap">{fmt.dayClock(item.at)}</td>
                        <td className="mono">{item.workspace || "-"}</td>
                        <td>{item.name}
                          {item.dept && <div className="hint">{item.dept}</div>}</td>
                        <td className="nowrap">{KIND_LABEL[item.kind] ?? item.kind}</td>
                        <td className="feedback-text">
                          {item.text || (item.hasText ? <span className="hint">관리자만 볼 수 있습니다</span> : <span className="hint">내용 없음</span>)}
                          {item.handledNote && <div className="hint">처리: {item.handledNote}</div>}
                        </td>
                        <td className="nowrap">
                          {item.handled
                            ? <><Chip tone="ok">반영됨</Chip>
                                <div className="hint">{item.handledBy}</div></>
                            : isAdmin
                              ? <button className="btn btn-sm" disabled={busy === item.id}
                                  onClick={() => markHandled(item.id)}>처리 표시</button>
                              : <Chip tone="watch">미처리</Chip>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="section-lead">아직 접수된 신고가 없습니다.</p>
            )}
          </Section>

          <Section
        title="기여도"
        lead={
          '정정 사항을 적어 보낸 순입니다. ' +
          '👍 개수로 줄을 세우면 많이 누른 사람이 위로 올라가고, ' +
          '실제로 고칠 거리를 준 사람이 묻힙니다.'
        }
      >
        {feedback.contributors.length ? (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>사람</th>
                  <th>부서</th>
                  <th>워크스페이스</th>
                  <th>정정 사항</th>
                  <th>신고</th>
                  <th>👍</th>
                </tr>
              </thead>
              <tbody>
                {feedback.contributors.map((c) => (
                  <tr key={c.actor}>
                    <td>{c.name}
                      {c.lastCorrection && <div className="hint">최근: {c.lastCorrection}</div>}</td>
                    <td>{c.dept || <span className="hint">매핑 없음</span>}</td>
                    <td className="mono">{c.workspaces.join(", ") || "-"}</td>
                    <td>{c.corrections.toLocaleString()}</td>
                    <td>{c.reports.toLocaleString()}</td>
                    <td>{c.praise.toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="section-lead">아직 접수된 피드백이 없습니다.</p>
        )}
          </Section>
        </>
      )}

      <p className="page-note">
        점검 시각 {new Date(r.checkedAt).toLocaleString('ko-KR')}
        {user.role !== 'admin' && ' · 담당 워크스페이스 범위로만 집계했습니다'}
      </p>
    </>
  )
}
