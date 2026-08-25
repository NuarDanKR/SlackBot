/** 수집 문서 열람 — 봇이 실제로 무엇을 쌓았는지 확인하는 화면입니다.
 *
 * 수집 경로는 봇 서버의 `ARCHIVE_DIR` 아래입니다.
 *   /var/lib/tybot/archive/channels/<워크스페이스>/<채널>.md
 *
 * ## 왜 열람을 제한하나
 * 이 문서들은 Slack 채널에 들어가 있는 사람만 볼 수 있던 대화입니다.
 * 콘솔에서 아무나 열 수 있으면 채널 멤버십으로 만든 권한 구분이 무의미해집니다.
 * 그래서 세 가지를 함께 겁니다.
 *   1. 원문 본문은 관리자에게만 보여 줍니다. 담당자는 목록과 수집 상태만 봅니다.
 *   2. 열람 사실을 감사 기록에 남깁니다.
 *   3. 화면에도 그 사실을 적어 둡니다 — 조용히 열리는 경로를 만들지 않습니다.
 */
import { useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import type { CollectedDoc, ConsoleUser } from '../types'
import { Frontmatter, Markdown } from '../components/Markdown'
import { Empty, Failed, Loading, PageHead, Section, agoLabel, fmt } from '../components/primitives'

interface ReadAuditEntry {
  at: string
  actor: string
  email: string
  path: string
}

function DocMeta({ doc }: { doc: CollectedDoc }) {
  return (
    <div className="metrics" style={{ borderTop: 0, paddingTop: 0 }}>
      <div>
        <div className="metric-k">원문</div>
        <div className="metric-v">
          {fmt.int(doc.lines)}
          <span className="unit">줄</span>
        </div>
      </div>
      <div>
        <div className="metric-k">첨부에서 추출</div>
        <div className="metric-v">
          {fmt.int(doc.attachmentLines)}
          <span className="unit">줄</span>
        </div>
      </div>
      <div>
        <div className="metric-k">파일 크기</div>
        <div className="metric-v" style={{ fontSize: 15 }}>
          {fmt.kb(doc.bytes)}
        </div>
      </div>
    </div>
  )
}

export function Collected({
  user,
  onToast,
}: {
  user: ConsoleUser
  onToast: (m: string) => void
}) {
  // 서버가 이미 권한 범위로 좁혀서 내려 줍니다.
  const res = useResource<{ docs: CollectedDoc[] }>('/api/collected')
  const isOwner = user.role === 'owner'
  const audit = useResource<{ entries: ReadAuditEntry[] }>(isOwner ? '/api/collected/audit' : null)

  const docs = res.data?.docs ?? []
  const [selected, setSelected] = useState('')
  // 연 문서의 본문. 목록에는 본문이 없고, 열 때만 따로 받아 옵니다.
  const [opened, setOpened] = useState<Record<string, string>>({})
  const [opening, setOpening] = useState(false)
  const [openError, setOpenError] = useState<string | null>(null)

  const doc = docs.find((d) => d.path === selected) ?? docs[0]
  const body = doc ? opened[doc.path] : undefined

  async function openDocument(target: CollectedDoc) {
    setOpening(true)
    setOpenError(null)
    try {
      const r = await api.get<{ path: string; content: string }>(
        `/api/collected/content?path=${encodeURIComponent(target.path)}`,
      )
      setOpened((o) => ({ ...o, [target.path]: r.content }))
      onToast(`${target.channel} 원문을 열었습니다. 열람 기록을 남겼습니다.`)
      audit.reload()
    } catch (e) {
      setOpenError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setOpening(false)
    }
  }

  const byWorkspace = docs.reduce<Record<string, CollectedDoc[]>>((acc, d) => {
    ;(acc[d.workspaceLabel] ??= []).push(d)
    return acc
  }, {})

  return (
    <>
      <PageHead
        crumb="데이터 · 수집 문서 열람"
        title="수집 문서 열람"
        note="봇이 채널에서 모아 저장한 문서를 그대로 확인할 수 있습니다. 수집이 잘 되고 있는지, 첨부 파일이 제대로 변환되었는지, 형식이 깨진 문서가 없는지 점검하는 화면입니다."
        aside={<span className="chip flat">문서 {docs.length}건</span>}
      />

      <div className="section">
        <div
          className={`callout ${isOwner ? 'warn' : 'info'}`}
        >
          <span>
            {isOwner ? (
              <>
                여기 보이는 내용은 <b>Slack 채널에 있던 실제 대화</b>입니다. 원문 열람은 관리자에게만
                열려 있고, <b>어느 문서를 언제 열었는지 감사 기록에 남습니다.</b> 필요한 확인만 하고
                내용을 콘솔 밖으로 옮기지 말아 주세요.
              </>
            ) : (
              <>
                담당 워크스페이스의 <b>수집 상태와 목록</b>을 볼 수 있습니다. 대화 원문은 채널
                구성원만 볼 수 있는 자료이므로 이 화면에서는 열리지 않습니다. 내용 확인이 필요하면
                해당 Slack 채널에서 직접 확인해 주세요.
              </>
            )}
          </span>
        </div>
      </div>

      {res.loading && <Loading what="문서 목록을" />}
      {res.error && (
        <div className="section">
          <Failed what="문서 목록을" detail={res.error.message} onRetry={res.reload} />
        </div>
      )}

      {doc ? (
        <Section
          title="문서 목록과 내용"
          note={`${doc.workspaceLabel} · ${agoLabel(doc.lastIngestedAt)} 수집`}
          lead="왼쪽에서 채널 문서를 고르면 오른쪽에 저장된 내용이 그대로 표시됩니다."
        >
          <div className="browser">
            <div className="browser-side">
              {Object.entries(byWorkspace).map(([label, list]) => (
                <div key={label}>
                  <div className="tree-group">{label}</div>
                  {list.map((d) => (
                    <button
                      key={d.path}
                      className={`tree-item ${d.path === selected ? 'is-active' : ''}`}
                      onClick={() => setSelected(d.path)}
                    >
                      <div className="tree-name">
                        {d.channel}
                        {d.schemaError ? ' · 형식 오류' : ''}
                      </div>
                      <div className="tree-meta">
                        {fmt.int(d.lines)}줄 · {fmt.day(d.lastIngestedAt)}
                      </div>
                    </button>
                  ))}
                </div>
              ))}
            </div>

            <div className="browser-main">
              <div className="browser-bar">
                <div>
                  <div className="card-title">{doc.channel}</div>
                  <div className="browser-path">archive/{doc.path}</div>
                </div>
                <div className="browser-tools">
                  {doc.visibility === 'public' ? (
                    <span className="chip info">워크스페이스 공개</span>
                  ) : (
                    <span className="chip flat">채널 구성원만</span>
                  )}
                  {doc.shareWith.length > 0 && (
                    <span className="chip brand">{doc.shareWith.join(', ')} 공유</span>
                  )}
                </div>
              </div>

              <div className="pane-body">
                <DocMeta doc={doc} />

                {doc.schemaError && (
                  <div className="notice bad" style={{ marginBottom: 16 }}>
                    <div className="notice-kind">형식 오류</div>
                    <div>
                      <div className="notice-title">이 문서는 답변 근거로 쓰이지 않습니다</div>
                      <div className="notice-detail">{doc.schemaError}</div>
                    </div>
                  </div>
                )}

                <Frontmatter source={body ?? ''} />

                {openError && (
                  <div className="notice bad" style={{ marginBottom: 14 }}>
                    <div>
                      <div className="notice-title">원문을 열지 못했습니다</div>
                      <div className="notice-detail">{openError}</div>
                    </div>
                  </div>
                )}

                {!isOwner ? (
                  <Empty
                    title="원문은 이 화면에서 열리지 않습니다"
                    note="대화 원문은 채널 구성원만 볼 수 있는 자료입니다. 위의 수집 상태로 정상 동작을 확인하고, 내용은 Slack 채널에서 직접 확인해 주세요."
                  />
                ) : body !== undefined ? (
                  <Markdown source={body} />
                ) : (
                  <div className="empty">
                    <div className="empty-title">원문을 열겠습니까?</div>
                    <p className="empty-note">
                      이 문서에는 Slack 채널의 실제 대화가 담겨 있습니다. 열람하면 감사 기록에
                      남습니다.
                    </p>
                    <div style={{ marginTop: 14 }}>
                      <button
                        className="btn btn-primary"
                        disabled={opening}
                        onClick={() => openDocument(doc)}
                      >
                        {opening ? '여는 중…' : '원문 열기'}
                      </button>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
        </Section>
      ) : (
        <Empty
          title="수집된 문서가 없습니다"
          note="채널에 봇을 초대하면 대화가 쌓이기 시작하고, 이 목록에 문서가 나타납니다."
        />
      )}

      {isOwner && (
        <Section
          title="원문 열람 기록"
          note="추가만 되고 지울 수 없습니다"
          lead="누가 어떤 문서를 언제 열었는지 남습니다. 이 화면에서 지울 수 없습니다."
        >
          <div className="card card-pad">
            {audit.error && (
              <Failed what="열람 기록을" detail={audit.error.message} onRetry={audit.reload} />
            )}
            {!audit.error && (audit.data?.entries.length ?? 0) === 0 && (
              <p className="field-help">아직 원문을 연 기록이 없습니다.</p>
            )}
            <div className="log">
              {(audit.data?.entries ?? []).map((a) => (
                <div className="log-row" key={a.at + a.path}>
                  <span className="log-when">{fmt.dayClock(a.at)}</span>
                  <span className="log-action">열람</span>
                  <span className="log-text">
                    <b>{a.actor}</b> — <span className="mono">{a.path}</span>
                  </span>
                </div>
              ))}
            </div>
          </div>
        </Section>
      )}
    </>
  )
}
