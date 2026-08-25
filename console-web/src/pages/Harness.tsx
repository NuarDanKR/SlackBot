/** 봇 규칙 편집 — 봇의 답변 규칙·업무 흐름을 담은 MD 파일을 콘솔에서 고칩니다.
 *
 * TYBot 은 두 층으로 움직입니다. 파이썬 코드가 수집·검색·권한을 통제하고,
 * MD 파일이 답변 규칙과 업무 흐름을 정합니다. 현업이 손대는 쪽은 대부분 MD 입니다.
 * 그래서 서버 파일을 직접 고치는 대신 여기서 편집하고, 관리자가 확인한 뒤 반영합니다.
 *
 * 저장 버튼이 곧 반영이 아닙니다 — **편집 내용은 승인 요청으로 올라갑니다.**
 */
import { useMemo, useState } from 'react'
import { useResource } from '../api/hooks'
import { harnessRequests } from '../mock/harness'
import type { ConsoleUser, HarnessFile } from '../types'
import { ApprovalBox, Checks, statusChip } from '../components/Approval'
import { Markdown } from '../components/Markdown'
import {
  Empty,
  Failed,
  Loading,
  MockBadge,
  PageHead,
  Section,
  fmt,
} from '../components/primitives'

const KIND_LABEL: Record<HarnessFile['kind'], string> = {
  rules: '답변 규칙',
  workflow: '업무 흐름',
  glossary: '용어 사전',
  prompt: '프롬프트',
}

/** 줄 단위로 무엇이 늘고 줄었는지 셉니다. 편집자가 자기 변경 규모를 알 수 있게 합니다. */
function countDiff(before: string, after: string) {
  const b = before.split('\n')
  const a = after.split('\n')
  const bSet = new Map<string, number>()
  for (const l of b) bSet.set(l, (bSet.get(l) ?? 0) + 1)
  let added = 0
  for (const l of a) {
    const n = bSet.get(l) ?? 0
    if (n > 0) bSet.set(l, n - 1)
    else added++
  }
  let removed = 0
  for (const n of bSet.values()) removed += n
  return { added, removed }
}

function Editor({ file, onToast }: { file: HarnessFile; onToast: (m: string) => void }) {
  const [draft, setDraft] = useState(file.content)
  const [reason, setReason] = useState('')
  const [submitted, setSubmitted] = useState(false)
  const [mode, setMode] = useState<'edit' | 'preview'>('edit')

  const diff = useMemo(() => countDiff(file.content, draft), [file.content, draft])
  const changed = diff.added > 0 || diff.removed > 0
  const canSubmit = changed && reason.trim().length >= 5 && !submitted
  const locked = file.pendingRequestId !== null

  return (
    <div className="browser-main">
      <div className="browser-bar">
        <div>
          <div className="card-title">{file.title}</div>
          <div className="browser-path">{file.path}</div>
        </div>
        <div className="browser-tools">
          <button
            className={`btn btn-sm ${mode === 'edit' ? '' : 'btn-quiet'}`}
            onClick={() => setMode('edit')}
          >
            편집
          </button>
          <button
            className={`btn btn-sm ${mode === 'preview' ? '' : 'btn-quiet'}`}
            onClick={() => setMode('preview')}
          >
            미리보기
          </button>
        </div>
      </div>

      {locked && (
        <div style={{ padding: '14px 18px 0' }}>
          <div className="callout warn">
            <span>
              이 파일에는 <b>승인 대기 중인 편집</b>이 이미 있습니다. 지금 저장하면 앞선 요청과
              충돌할 수 있으니, 아래 승인 요청이 처리된 뒤에 수정해 주세요.
            </span>
          </div>
        </div>
      )}

      {mode === 'edit' ? (
        <div className="pane-body">
          <textarea
            className="editor"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            spellCheck={false}
            aria-label={`${file.title} 편집`}
          />
        </div>
      ) : (
        <div className="pane-body">
          <Markdown source={draft} />
        </div>
      )}

      <div style={{ borderTop: '1px solid var(--line)', padding: '16px 18px' }}>
        {submitted ? (
          <div className="approve-status wait">
            승인 요청을 올렸습니다. 관리자가 확인하면 반영되고 Slack 으로 알려 드립니다.
          </div>
        ) : (
          <>
            <div className="field">
              <label className="field-label" htmlFor="harness-reason">
                무엇을 왜 바꾸는지 적어 주세요
              </label>
              <input
                id="harness-reason"
                className="input"
                placeholder="예: 부가세 표기가 없는 금액을 봇이 단정해서 답한 사례가 있어 규칙을 한 줄 추가했습니다"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
              <span className="field-help">
                승인하는 사람이 판단할 근거가 됩니다. 다섯 글자 이상 적어 주세요.
              </span>
            </div>
            <div className="form-row">
              <button
                className="btn btn-primary"
                disabled={!canSubmit}
                onClick={() => {
                  setSubmitted(true)
                  onToast(`${file.title} 수정안을 승인 요청으로 올렸습니다.`)
                }}
              >
                승인 요청 보내기
              </button>
              <button
                className="btn btn-quiet"
                disabled={!changed}
                onClick={() => {
                  setDraft(file.content)
                  setReason('')
                }}
              >
                되돌리기
              </button>
              <span className="field-help">
                {changed ? (
                  <>
                    <b>{diff.added}줄 추가</b>
                    {diff.removed ? `, ${diff.removed}줄 삭제` : ''} — 아직 봇에 반영되지 않았습니다.
                  </>
                ) : (
                  '아직 바뀐 내용이 없습니다.'
                )}
              </span>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

export function Harness({
  user,
  onToast,
}: {
  user: ConsoleUser
  onToast: (m: string) => void
}) {
  // 서버가 이미 권한 범위로 좁혀서 내려 줍니다.
  const res = useResource<{ files: HarnessFile[] }>('/api/harness')
  const files = res.data?.files ?? []
  const [selected, setSelected] = useState('')
  const file = files.find((f) => f.path === selected) ?? files[0]

  // 승인 요청은 아직 서버 API 가 없습니다(BACKLOG B-11).
  const requests = harnessRequests.filter(
    (r) => user.role === 'owner' || user.workspaces.includes(r.workspace),
  )

  const byWorkspace = files.reduce<Record<string, HarnessFile[]>>((acc, f) => {
    ;(acc[f.workspaceLabel] ??= []).push(f)
    return acc
  }, {})

  return (
    <>
      <PageHead
        crumb="관리 · 봇 규칙 편집"
        title="봇 규칙 편집"
        note="봇이 어떻게 답할지 정하는 규칙 문서를 여기서 고칩니다. 답변 규칙, 업무 흐름, 용어 사전이 여기에 들어갑니다. 저장하면 바로 반영되는 것이 아니라 승인 요청으로 올라가고, 관리자가 확인한 뒤 봇에 적용됩니다."
        aside={<span className="chip flat">파일 {files.length}개</span>}
      />

      {res.loading && <Loading what="규칙 문서를" />}
      {res.error && (
        <div className="section">
          <Failed what="규칙 문서를" detail={res.error.message} onRetry={res.reload} />
        </div>
      )}

      {file ? (
        <Section
          title="규칙 문서"
          note={`${KIND_LABEL[file.kind]} · ${file.workspaceLabel}`}
          lead="왼쪽에서 파일을 고르고 오른쪽에서 고칩니다. 미리보기로 봇이 읽을 최종 모양을 확인할 수 있습니다."
        >
          <div className="browser">
            <div className="browser-side">
              {Object.entries(byWorkspace).map(([label, list]) => (
                <div key={label}>
                  <div className="tree-group">{label}</div>
                  {list.map((f) => (
                    <button
                      key={f.path}
                      className={`tree-item ${f.path === selected ? 'is-active' : ''}`}
                      onClick={() => setSelected(f.path)}
                    >
                      <div className="tree-name">
                        {f.title}
                        {f.pendingRequestId ? ' · 승인 대기' : ''}
                      </div>
                      <div className="tree-meta">
                        {KIND_LABEL[f.kind]} · {fmt.day(f.updatedAt)} {f.updatedBy}
                      </div>
                    </button>
                  ))}
                </div>
              ))}
            </div>
            <Editor file={file} onToast={onToast} />
          </div>
        </Section>
      ) : (
        <Empty
          title="편집할 규칙 문서가 없습니다"
          note="담당 워크스페이스가 배정되면 이 목록에 규칙 문서가 나타납니다."
        />
      )}

      <Section
        title="승인 대기 중인 수정안"
        aside={<MockBadge />}
        lead={
          user.role === 'owner'
            ? '규칙 문서는 봇의 답변을 직접 바꿉니다. 반영 전에 무엇이 어떻게 달라지는지 확인해 주세요.'
            : '올린 수정안이 관리자 확인을 기다리는 중입니다.'
        }
      >
        {requests.length ? (
          requests.map((r) => <HarnessRequestCard key={r.id} id={r.id} user={user} onToast={onToast} />)
        ) : (
          <Empty
            title="대기 중인 수정안이 없습니다"
            note="규칙 문서를 고쳐 승인 요청을 보내면 여기에 표시됩니다."
          />
        )}
      </Section>
    </>
  )
}

function HarnessRequestCard({
  id,
  user,
  onToast,
}: {
  id: string
  user: ConsoleUser
  onToast: (m: string) => void
}) {
  const r = harnessRequests.find((x) => x.id === id)!
  const [approved, setApproved] = useState(false)

  return (
    <article className="request">
      <div className="request-head">
        <div>
          <div className="request-title">
            {r.title} <span style={{ color: 'var(--text-3)', fontWeight: 400 }}>수정안</span>
          </div>
          <div className="request-meta">
            <span>{r.workspaceLabel}</span>
            <span className="sep">·</span>
            <span className="mono">{r.path}</span>
            <span className="sep">·</span>
            <span>
              {r.added}줄 추가
              {r.removed ? `, ${r.removed}줄 삭제` : ''}
            </span>
          </div>
        </div>
        {statusChip(r.checks, approved)}
      </div>

      <div className="request-body">
        <div className="request-col">
          <div className="col-label">요청자가 적은 변경 이유</div>
          <p className="hint" style={{ fontSize: 13 }}>
            {r.reason}
          </p>

          <div className="files">
            <Checks checks={r.checks} label="자동 검사" />
          </div>

          <div className="files">
            <div className="col-label">추가되는 내용</div>
            <div className="card" style={{ background: 'var(--sunk)', boxShadow: 'none' }}>
              <div className="pane-body">
                <Markdown source={addedPart(r.before, r.after)} />
              </div>
            </div>
          </div>
        </div>

        <div className="request-col">
          <div className="col-label">승인</div>
          <ApprovalBox
            user={user}
            checks={r.checks}
            approved={approved}
            requester={r.requester}
            requestedAt={fmt.dayClock(r.requestedAt)}
            approveLabel="승인하고 반영"
            onApprove={() => {
              setApproved(true)
              onToast(`${r.title} 수정안을 승인했습니다. 봇이 다음 답변부터 새 규칙을 씁니다.`)
            }}
            onReject={() => onToast(`${r.title} 수정안을 반려했습니다.`)}
            afterNote="승인하면 규칙 파일이 교체되고, 봇은 다음 답변부터 새 규칙을 따릅니다. 이전 내용은 이력에 남아 언제든 되돌릴 수 있습니다."
            blockedNote="문서 형식이나 금지 항목 검사에서 문제가 발견되었습니다. 요청자가 고쳐서 다시 올려야 합니다."
          />
        </div>
      </div>
    </article>
  )
}

/** before 에 없던 줄만 모아 보여 줍니다 — 승인자가 읽을 것은 '늘어난 규칙'입니다. */
function addedPart(before: string, after: string): string {
  const b = new Set(before.split('\n'))
  const lines = after.split('\n').filter((l) => !b.has(l) && l.trim())
  if (!lines.length) return '추가된 줄이 없습니다. 삭제나 순서 변경만 있습니다.'
  // 코드블록으로 감쌉니다 — 마크다운으로 렌더하면 `5.` 로 시작하는 줄이 `1.` 로 번호가 다시
  // 매겨져서, 몇 번째 규칙이 추가되는지 승인자가 오해할 수 있습니다.
  return ['```', ...lines, '```'].join('\n')
}
