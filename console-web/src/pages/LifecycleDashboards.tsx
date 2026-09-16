import { useEffect, useState } from 'react'
import { useResource } from '../api/hooks'
import { Chip, Failed, Loading, Metric, MiniBars, PageHead, Section, fmt } from '../components/primitives'
import type { AnswerRecordsResponse, AuditEvent, ConsoleUser } from '../types'
import { withQuery } from '../navigation'
import { DateRangeFilter, periodLabel } from '../components/DateRangeFilter'

type Navigate = (path: string) => void

function ActionRow({ title, detail, tone = 'plain', onClick }: {
  title: string; detail: string; tone?: 'plain' | 'watch' | 'bad' | 'ok'; onClick: () => void
}) {
  return (
    <button className="action-row" type="button" onClick={onClick}>
      <span><strong>{title}</strong><small>{detail}</small></span>
      <Chip tone={tone}>{tone === 'ok' ? '정상' : tone === 'plain' ? '보기' : '확인 필요'}</Chip>
    </button>
  )
}

export function AnswerDashboard({ user, query, navigate }: { user: ConsoleUser; query: URLSearchParams; navigate: Navigate }) {
  const start = query.get('start') ?? ''
  const end = query.get('end') ?? ''
  const workspace = query.get('workspace') ?? ''
  const result = query.get('result') ?? ''
  const asker = query.get('asker') ?? ''
  const [workspaceDraft, setWorkspaceDraft] = useState(workspace)
  const [resultDraft, setResultDraft] = useState(result)
  const [askerDraft, setAskerDraft] = useState(asker)
  useEffect(() => { setWorkspaceDraft(workspace); setResultDraft(result); setAskerDraft(asker) }, [workspace, result, asker])
  const params = new URLSearchParams({ ...(start && { start }), ...(end && { end }), ...(workspace && { workspace }), ...(result && { result }), ...(asker && { asker }) })
  const res = useResource<AnswerRecordsResponse>(`/api/answer-records?${params}`)
  if (res.loading) return <Loading what="답변 대시보드를" />
  if (res.error || !res.data) return <Failed what="답변 대시보드를" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const d = res.data
  const s = d.summary
  return <>
    <PageHead crumb="답변" title="답변 현황" note="답변 개요, 품질 지표와 질문 처리 기록을 같은 기간·범위로 확인합니다." aside={<><Chip tone={s.errors ? 'watch' : 'ok'}>기간 질문 {fmt.int(s.questions)}건</Chip><Chip tone={d.view === 'all' ? 'brand' : 'info'}>{d.view === 'all' ? '관리자 전체 범위' : '본인·검토 채널 범위'}</Chip></>} />
    {!d.identityLinked && user.role !== 'admin' && <div className="notice warn"><div><div className="notice-title">Slack 사용자 연결을 확인할 수 없습니다</div><div className="notice-detail">회사 이메일과 Slack 사용자 매핑이 확인되면 본인 질문과 담당 검토 채널의 답변이 표시됩니다.</div></div></div>}
    <Section title="조회 기간" note={periodLabel(d.periodStart, d.periodEnd)}>
      <DateRangeFilter path="/answer" query={query} navigate={navigate} defaultStart={d.periodStart} defaultEnd={d.periodEnd} today={d.today} preserve={{ workspace, result, asker }} />
    </Section>
    <Section title="기간 답변 요약" lead="아래 품질 분포와 처리 기록도 같은 필터 범위를 사용합니다.">
      <div className="metrics overview-metrics"><Metric k="질문" v={fmt.int(s.questions)} unit="건" /><Metric k="근거 확보율" v={s.groundedRate == null ? '-' : `${Math.round(s.groundedRate * 100)}%`} /><Metric k="오류" v={fmt.int(s.errors)} unit="건" /><Metric k="15초 초과" v={fmt.int(s.slowAnswers)} unit="건" /><Metric k="사용액" v={fmt.usd(s.spentUsd)} /></div>
    </Section>
    <Section title="답변 품질 분포" lead="한 질문이 근거 없음·오류·지연에 동시에 포함될 수 있습니다. 막대를 선택하면 해당 원본 목록으로 이동합니다.">
      <MiniBars label="답변 품질 분포" items={[{ label: '근거 있음', value: s.grounded, tone: 'ok' }, { label: '근거 없음', value: s.noHits, tone: 'warn' }, { label: '오류', value: s.errors, tone: 'bad' }, { label: '15초 초과', value: s.slowAnswers, tone: 'warn' }]} />
      <div className="quality-links"><button className="btn btn-sm" type="button" onClick={() => navigate(withQuery('/answer/records', { start: d.periodStart, end: d.periodEnd, result: 'no_hits' }))}>근거 없는 답변 원본</button><button className="btn btn-sm" type="button" onClick={() => navigate(withQuery('/answer/records', { start: d.periodStart, end: d.periodEnd, result: 'error' }))}>오류 답변 원본</button><button className="btn btn-sm" type="button" onClick={() => navigate(withQuery('/answer/records', { start: d.periodStart, end: d.periodEnd, result: 'slow' }))}>느린 답변 원본</button></div>
    </Section>
    <Section title="질문 처리 기록" note={`${d.records.length}건`} lead="행을 선택하면 질문·답변 원본과 출처, 연결된 피드백을 확인합니다.">
      <form className="filter-row" onSubmit={(event) => { event.preventDefault(); navigate(withQuery('/answer', { start: d.periodStart, end: d.periodEnd, workspace: workspaceDraft.trim(), result: resultDraft, asker: askerDraft.trim() })) }}>
        <div className="field"><label className="field-label" htmlFor="answer-workspace">워크스페이스</label><input id="answer-workspace" className="input" placeholder="예: tyit" value={workspaceDraft} onChange={(event) => setWorkspaceDraft(event.target.value)} /></div>
        <div className="field"><label className="field-label" htmlFor="answer-result">처리 결과</label><select id="answer-result" className="input" value={resultDraft} onChange={(event) => setResultDraft(event.target.value)}><option value="">모든 결과</option><option value="answered">답변 완료</option><option value="no_hits">근거 없음</option><option value="error">오류</option><option value="slow">15초 초과</option><option value="feedback">피드백 있음</option></select></div>
        {user.role === 'admin' && <div className="field"><label className="field-label" htmlFor="answer-asker">질문자</label><input id="answer-asker" className="input" placeholder="이름 또는 Slack ID" value={askerDraft} onChange={(event) => setAskerDraft(event.target.value)} /></div>}
        <div className="filter-actions"><button className="btn btn-primary btn-sm" type="submit">조회</button>{(workspace || result || asker) && <button className="btn btn-sm btn-quiet" type="button" onClick={() => navigate(withQuery('/answer', { start: d.periodStart, end: d.periodEnd }))}>초기화</button>}</div>
      </form>
      <div className="table-wrap"><table className="table"><thead><tr><th>시각</th><th>질문자</th><th>워크스페이스·채널</th><th>분류</th><th>결과</th><th className="num">근거</th><th>모델</th><th className="num">소요 시간</th><th>원본</th></tr></thead><tbody>{d.records.map((row) => <tr key={row.recordKey} className={row.reason === 'error' ? 'is-error-row' : undefined}><td>{fmt.dayClock(row.at)}</td><td>{row.asker}</td><td>{row.workspace}<div className="subtle">{row.channel || '-'}</div></td><td className="mono">{row.intent}/{row.source}</td><td>{row.reason}{row.hasFeedback && <div className="subtle">피드백 있음</div>}</td><td className="num">{row.hits || '-'}</td><td className="mono">{row.model}</td><td className="num">{fmt.ms(row.ms)}</td><td><button className="table-link" type="button" onClick={() => navigate(withQuery('/answer/records', { record: row.recordKey, start: d.periodStart, end: d.periodEnd }))}>질문·답변 보기</button></td></tr>)}</tbody></table></div>
    </Section>
  </>
}

interface OperationsData {
  commands: { level: string; problems: string[] }
  disabledTimers: number
  deployment: { state: string; message?: string }
  specialistErrors: number
}

/**
 * 문제 패킷을 내 PC 로 받는다.
 *
 * 같은 보고서를 서버에서 만드는 CLI 가 이미 있다. 그런데 그 파일을 쓰려면 서버에
 * 들어가 FTP 로 꺼내 와야 했고, 그 몇 걸음 때문에 **실제로는 아무도 안 꺼냈다.**
 * 문제를 모아 두고 아무도 읽지 않는 상태가 제일 나쁘다.
 *
 * `fetch` + Blob 을 쓰는 이유: `<a href>` 로 열면 세션 쿠키는 따라가지만 **실패를
 * 알 수 없다.** 403·503 이 나도 브라우저는 그 오류 본문을 파일로 저장해 버리고,
 * 사람은 받은 파일을 열어 보고 나서야 안다.
 */
function IssuePacketDownload() {
  const [days, setDays] = useState('7')
  const [workspace, setWorkspace] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState('')

  async function download() {
    if (busy) return
    setBusy(true); setError(null); setDone('')
    try {
      const params = new URLSearchParams({ days, ...(workspace.trim() && { workspace: workspace.trim() }) })
      const response = await fetch(`/api/answer-issues/export?${params}`, { credentials: 'same-origin' })
      if (!response.ok) {
        // 오류 본문은 JSON 이다. 그걸 파일로 저장하면 사람은 열어 보고 나서야 안다.
        const detail = await response.json().catch(() => null)
        throw new Error(detail?.detail ?? `요청이 실패했습니다 (${response.status})`)
      }
      // 서버가 지은 이름을 그대로 쓴다 — 기간·범위가 이름에 들어 있어서,
      // 여러 장을 받아 두었을 때 파일명만으로 어느 것이 무엇인지 안다.
      const disposition = response.headers.get('Content-Disposition') ?? ''
      const name = /filename="([^"]+)"/.exec(disposition)?.[1] ?? 'answer-issues.md'
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = name
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
      setDone(name)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Section
      title="문제 패킷 내려받기"
      lead="최근 답변의 오류·근거 없음·지연·전문 봇 실패와 사용자 피드백 원문을 Markdown 한 장으로 묶어 받습니다. 받은 파일을 저장소의 qa-issues/ 폴더에 넣고 Claude 나 Codex 에게 넘기면 그대로 분류·수정에 들어갑니다."
    >
      <div className="card card-pad">
        <div className="form-grid">
          <div className="field">
            <label className="field-label" htmlFor="packet-days">조회 일수</label>
            <input id="packet-days" className="input mono" type="number" min="1" max="90"
              value={days} onChange={(event) => setDays(event.target.value)} />
            <span className="field-help">오늘부터 거슬러 셉니다. 기본 7일.</span>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="packet-workspace">워크스페이스</label>
            <input id="packet-workspace" className="input" placeholder="비워 두면 전체"
              value={workspace} onChange={(event) => setWorkspace(event.target.value)} />
            <span className="field-help">예: tyit</span>
          </div>
        </div>

        <div className="form-row">
          <button className="btn btn-primary" type="button" disabled={busy} onClick={() => { void download() }}>
            {busy ? '만드는 중…' : '문제 패킷 내려받기'}
          </button>
        </div>

        {done && <p className="note">{done} 를 내려받았습니다. 저장소의 qa-issues/ 폴더로 옮기세요.</p>}
        {error && (
          <div className="notice bad" style={{ marginTop: 14 }}>
            <div className="notice-kind">내려받기 실패</div>
            <div><div className="notice-title">문제 패킷을 만들지 못했습니다</div>
              <div className="notice-detail">{error}</div></div>
          </div>
        )}

        <p className="note">
          이 파일에는 <strong>사내 질문·답변과 피드백 원문</strong>이 들어 있습니다.
          저장소에 커밋하거나 공개 경로에 두지 마세요 — qa-issues/ 는 .gitignore 에 있습니다.
          누가 언제 받았는지는 감사 기록에 남습니다.
        </p>
      </div>
    </Section>
  )
}

export function OperationsDashboard({ user, navigate }: { user: ConsoleUser; navigate: Navigate }) {
  const res = useResource<OperationsData>('/api/dashboards/operations')
  if (res.loading) return <Loading what="운영 대시보드를" />
  if (res.error || !res.data) return <Failed what="운영 대시보드를" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const d = res.data
  return <>
    <PageHead crumb="운영" title="운영 현황" note="서비스, 명령, 배치, 배포와 전문 봇의 운영 상태를 확인합니다." />
    <Section title="운영 상태"><div className="action-list">
      <ActionRow title="Slack 명령" detail={`등록 불일치 ${d.commands.problems.length}건`} tone={d.commands.problems.length ? 'bad' : 'ok'} onClick={() => navigate('/manage/commands')} />
      <ActionRow title="전문 봇" detail={`장애 ${d.specialistErrors}개`} tone={d.specialistErrors ? 'bad' : 'plain'} onClick={() => navigate(withQuery('/manage/specialists', { state: d.specialistErrors ? 'error' : null }))} />
      {user.role === 'admin' && <ActionRow title="배치" detail={`사용 중지 ${d.disabledTimers}개`} tone={d.disabledTimers ? 'watch' : 'ok'} onClick={() => navigate(withQuery('/manage/batches', { state: d.disabledTimers ? 'disabled' : null }))} />}
      {user.role === 'admin' && <ActionRow title="배포" detail={d.deployment.message || d.deployment.state} tone={d.deployment.state === 'failed' ? 'bad' : 'plain'} onClick={() => navigate(withQuery('/manage/deploy', { state: d.deployment.state === 'failed' ? 'failed' : null }))} />}
      {user.role === 'admin' && <ActionRow title="워크스페이스" detail="등록, 사용 중지와 Slack 토큰을 관리합니다." onClick={() => navigate('/manage/workspaces')} />}
      {user.role === 'admin' && <ActionRow title="환경 설정" detail="공통 동작과 LLM API 키를 관리합니다." onClick={() => navigate('/manage/environment')} />}
    </div></Section>
    {user.role === 'admin' && <IssuePacketDownload />}
  </>
}

interface ConsoleData { users: number; admins: number; pendingApprovals: number; recentAudit: AuditEvent[] }
export function ConsoleDashboard({ navigate }: { navigate: Navigate }) {
  const res = useResource<ConsoleData>('/api/dashboards/console')
  if (res.loading) return <Loading what="콘솔 대시보드를" />
  if (res.error || !res.data) return <Failed what="콘솔 대시보드를" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const d = res.data
  return <>
    <PageHead crumb="콘솔 관리" title="권한 현황" note="콘솔 접근 권한과 관리 작업의 흔적을 확인합니다." />
    <Section title="접근과 승인"><div className="metrics overview-metrics"><Metric k="사용자" v={fmt.int(d.users)} unit="명" /><Metric k="관리자" v={fmt.int(d.admins)} unit="명" /><Metric k="승인 대기" v={fmt.int(d.pendingApprovals)} unit="건" /></div></Section>
    <Section title="관리 바로가기"><div className="action-list">
      <ActionRow title="콘솔 사용자 관리" detail="계정, 역할과 워크스페이스 범위를 관리합니다." onClick={() => navigate('/console/users')} />
      <ActionRow title="감사 기록" detail={`최근 이벤트 ${d.recentAudit.length}건`} onClick={() => navigate('/console/audit')} />
    </div></Section>
  </>
}
