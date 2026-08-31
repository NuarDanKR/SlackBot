/** Slack 봇 만들기부터 콘솔 등록까지의 안내.
 *
 * 이 화면을 쓰는 사람은 대부분 Slack 앱을 처음 만듭니다. 그래서 **줄글로 설명하지 않고
 * 번호가 매겨진 짧은 동작으로 끊어서** 적습니다. 한 줄에 하나씩만 하게 하는 것이 목적입니다.
 *
 * 기본은 접힌 상태입니다. 이미 아는 사람에게는 등록 폼이 먼저 보여야 하고,
 * 처음인 사람은 + 를 눌러 펼치면 됩니다. 다만 매니페스트는 **펼친 채로** 둡니다 —
 * 접혀 있으면 못 찾습니다.
 *
 * 매니페스트는 **서버가 저장소 파일을 읽어 내려줍니다**(`GET /api/manifest`).
 * 예전에는 같은 내용이 이 파일에도 상수로 있었고, 기능을 더할 때마다 두 곳을 함께
 * 고쳐야 했습니다. 한쪽만 고치면 이 화면을 보고 만든 앱에 권한이 빠져 봇이 오류 없이
 * 반쪽만 동작했습니다. 이제 고칠 곳은 `docs/pilot/slack-app-manifest.yaml` 하나입니다.
 *
 * 화면 캡처는 `public/guide/` 에 넣으면 각 단계에 붙습니다(BACKLOG B-18).
 * Slack 앱 관리 화면은 로그인해야 보이므로 캡처는 사람이 찍어야 합니다.
 */
import { useEffect, useState } from 'react'

import { fetchManifest } from '../api/client'


function CodeBlock({ label, code }: { label: string; code: string }) {
  const [copied, setCopied] = useState(false)

  async function copy() {
    // 콘솔을 http 로 열면 navigator.clipboard 가 보안 컨텍스트 제약으로 동작하지 않을 수
    // 있으므로, 실패하면 텍스트를 선택해 줍니다.
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      const el = document.getElementById(`code-${label}`)
      if (el) {
        const range = document.createRange()
        range.selectNodeContents(el)
        const sel = window.getSelection()
        sel?.removeAllRanges()
        sel?.addRange(range)
      }
    }
  }

  return (
    <div className="code-block">
      <div className="code-bar">
        <span className="code-label">{label}</span>
        <button className="btn btn-sm btn-quiet" onClick={copy}>
          {copied ? '복사했습니다' : '전체 복사'}
        </button>
      </div>
      <pre className="code-body">
        <code id={`code-${label}`}>{code}</code>
      </pre>
    </div>
  )
}

/** 화면 캡처. 파일이 없으면 아무것도 그리지 않습니다(캡처는 나중에 채웁니다). */
/** 저장소의 매니페스트를 받아 보여 줍니다.
 *
 * 실패해도 **낡은 사본을 대신 보여 주지 않습니다.** 그것이 정확히 예전 문제였습니다 —
 * 화면과 저장소가 다른데 사람은 알 수 없었습니다. 못 읽으면 못 읽었다고 말합니다.
 */
function ManifestBlock() {
  const [code, setCode] = useState<string | null>(null)
  const [meta, setMeta] = useState<{ updatedAt: string; sha256: string } | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    fetchManifest()
      .then((m) => {
        if (!alive) return
        setCode(m.content)
        setMeta({ updatedAt: m.updatedAt, sha256: m.sha256 })
      })
      .catch((e: unknown) => {
        if (!alive) return
        setError(e instanceof Error ? e.message : '매니페스트를 불러오지 못했습니다.')
      })
    return () => {
      alive = false
    }
  }, [])

  if (error) {
    return (
      <div className="rule bad">
        <span className="rule-mark">불러오기 실패</span>
        <span>
          {error}
          <br />
          저장소의 <code>docs/pilot/slack-app-manifest.yaml</code> 을 직접 열어 복사해 주세요.
        </span>
      </div>
    )
  }
  if (code === null) return <p className="muted">매니페스트를 불러오는 중…</p>

  return (
    <>
      <CodeBlock label="manifest.yaml" code={code} />
      {meta && (
        <p className="muted">
          저장소 파일 기준 · 수정 {meta.updatedAt} · 체크섬 <code>{meta.sha256}</code>
        </p>
      )}
    </>
  )
}

function Shot({ src, alt }: { src: string; alt: string }) {
  const [ok, setOk] = useState(true)
  if (!ok) return null
  return (
    <figure className="shot">
      <img src={src} alt={alt} loading="lazy" onError={() => setOk(false)} />
      <figcaption>{alt}</figcaption>
    </figure>
  )
}

function Step({
  n,
  title,
  where,
  children,
}: {
  n: number
  title: string
  /** 어느 화면에서 하는 일인지 — 매번 "여기가 어디지"를 묻지 않게 합니다 */
  where: string
  children: React.ReactNode
}) {
  return (
    <li className="step">
      <span className="step-num" aria-hidden="true">
        {n}
      </span>
      <div className="step-body">
        <div className="step-head">
          <span className="step-title">{title}</span>
          <span className="step-where">{where}</span>
        </div>
        <div className="step-text">{children}</div>
      </div>
    </li>
  )
}

/** 번호가 매겨진 동작 목록. 한 줄에 하나씩만 시킵니다. */
function Todo({ items }: { items: React.ReactNode[] }) {
  return (
    <ol className="todo">
      {items.map((it, i) => (
        <li key={i}>
          <span className="todo-num">({i + 1})</span>
          <span className="todo-text">{it}</span>
        </li>
      ))}
    </ol>
  )
}

export function SetupGuide() {
  return (
    <details className="guide">
      <summary className="guide-sum">
        <span className="guide-plus" aria-hidden="true" />
        <span>
          <span className="guide-title">Slack 봇을 처음 만드시나요? 순서대로 안내합니다</span>
          <span className="guide-sub">
            앱 만들기 · 토큰 두 개 받기 · 콘솔 등록 · 채널 초대 · 동작 확인 — 약 10분
          </span>
        </span>
      </summary>

      <div className="guide-body">
        <div className="callout warn">
          <span>
            <b>토큰은 채팅·메일·이슈에 붙여넣지 마세요.</b> 한 번 노출되면 그 워크스페이스의 모든
            대화를 읽을 수 있는 값입니다. 발급한 창에서 바로 이 화면에 붙여넣고, 실수로 공유했다면
            Slack 앱 설정에서 폐기(Revoke)한 뒤 다시 발급해 주세요.
          </span>
        </div>

        <ol className="steps">
          <Step n={1} title="Slack 앱 만들기" where="api.slack.com/apps">
            <Todo
              items={[
                <>
                  <a href="https://api.slack.com/apps" target="_blank" rel="noreferrer">
                    api.slack.com/apps
                  </a>{' '}
                  접속
                </>,
                <>
                  <b>Create New App</b> → <b>From an app manifest</b>
                </>,
                <>본인이 생성한 워크스페이스 선택</>,
                <>
                  <b>YAML</b> 선택 후 아래 매니페스트 복사·붙여넣기
                </>,
                <>
                  <b>Next</b> → <b>Create</b>
                </>,
              ]}
            />
            <Shot src="/guide/01-create-app.png" alt="Create New App → From an app manifest 선택 화면" />

            <ManifestBlock />

            <div className="rule-list">
              <div className="rule ok">
                <span className="rule-mark">수정 가능</span>
                <span>
                  <code>name</code> (앱 이름) · <code>description</code> (설명)
                </span>
              </div>
              <div className="rule ok">
                <span className="rule-mark">수정 가능</span>
                <span>
                  <code>display_name</code> — Slack 에서 부를 이름입니다. 영어 소문자를 권합니다
                  (예: <code>tybot</code>).
                </span>
              </div>
              <div className="rule bad">
                <span className="rule-mark">수정 금지</span>
                <span>
                  그 외 <b>전부</b>. 권한(scopes)과 이벤트(bot_events)는 하나만 빠져도 봇이 오류 없이
                  반쪽만 동작합니다.
                </span>
              </div>
            </div>

            <details className="sub">
              <summary>각 권한이 왜 필요한지</summary>
              <div className="scope-why">
                <div className="scope-row">
                  <code>files:read</code>
                  <span>
                    첨부 파일을 내려받습니다. 빠지면 파일 대신 로그인 화면이 내려와, 오류 없이 첨부만
                    통째로 누락됩니다.
                  </span>
                </div>
                <div className="scope-row">
                  <code>channels:join</code>
                  <span>
                    공개 채널에 봇이 스스로 들어갑니다. 빠지면 일부 채널만 수집되어 "왜 이 채널은 안
                    나오지" 상황이 생깁니다.
                  </span>
                </div>
                <div className="scope-row">
                  <code>canvases:read</code>
                  <span>
                    채널 캔버스 본문을 수집합니다. 빠지면 캔버스가 <b>미변환</b> 으로만 기록되고
                    내용은 들어가지 않습니다.
                  </span>
                </div>
                <div className="scope-row">
                  <code>channel_created</code> · <code>channel_rename</code>
                  <span>
                    채널을 새로 만들거나 이름을 바꾼 <b>그 순간</b> 봇이 규칙을 확인해 참여합니다.
                    빠지면 다음 재기동 때까지 그 채널이 수집되지 않습니다.
                  </span>
                </div>
                <div className="scope-row">
                  <code>commands</code> · <code>channels:manage</code> · <code>groups:write</code>
                  <span>
                    <code>/채널</code> 명령으로 규칙에 맞는 업무 채널을 만들고 이름을 고칩니다.
                    비공개 채널은 봇이 스스로 들어갈 수 없으므로, 만들 때 함께 넣는 이 경로가 필요합니다.
                  </span>
                </div>
                <div className="scope-row">
                  <code>interactivity.is_enabled</code>
                  <span>
                    전역 바로가기와 입력 창을 씁니다. <b>Socket Mode 라 Request URL 은 입력하지
                    않습니다</b> — 서버에 외부에서 들어오는 포트는 여전히 열지 않습니다.
                  </span>
                </div>
                <div className="scope-row">
                  <code>socket_mode_enabled</code>
                  <span>
                    봇이 Slack 으로 나가는 방식으로만 연결합니다. 서버에 외부에서 들어오는 포트를
                    열지 않기 위한 설정이라 반드시 켜져 있어야 합니다.
                  </span>
                </div>
                <div className="scope-row">
                  <code>messages_tab_read_only_enabled</code>
                  <span>봇에게 DM 을 보낼 수 있게 합니다. 켜져 있으면 DM 입력창이 잠깁니다.</span>
                </div>
              </div>
            </details>
          </Step>

          <Step n={2} title="앱 토큰 받기 (xapp-)" where="Basic Information">
            <Todo
              items={[
                <>
                  왼쪽 메뉴에서 <b>Basic Information</b> 클릭
                </>,
                <>
                  <b>화면을 아래로 한참 내립니다.</b> <b>App-Level Tokens</b> 항목은 페이지 중간
                  아래쪽에 있어 첫 화면에서는 보이지 않습니다.
                </>,
                <>
                  <b>Generate Token and Scopes</b> 클릭
                </>,
                <>
                  Token Name 에 아무 이름 입력 (예: <code>socket</code>)
                </>,
                <>
                  <b>Add Scope</b> → <code>connections:write</code> 선택
                </>,
                <>
                  <b>Generate</b> 클릭
                </>,
                <>
                  <code>xapp-</code> 로 시작하는 값을 복사 → 아래 등록 폼의 <b>앱 토큰</b> 칸에
                  붙여넣기
                </>,
              ]}
            />
            <Shot src="/guide/02-app-level-tokens.png" alt="Basic Information 아래쪽의 App-Level Tokens 위치" />
            <p className="step-note">
              이 창을 닫으면 토큰을 다시 볼 수 없습니다. 지금 바로 붙여넣어 주세요.
            </p>
          </Step>

          <Step n={3} title="워크스페이스에 설치하고 봇 토큰 받기 (xoxb-)" where="OAuth &amp; Permissions">
            <Todo
              items={[
                <>
                  왼쪽 메뉴에서 <b>OAuth &amp; Permissions</b> 클릭
                </>,
                <>
                  <b>Install to Workspace</b> 클릭
                </>,
                <>
                  권한 요청 화면에서 <b>허용</b>
                </>,
                <>
                  화면 위쪽 <b>Bot User OAuth Token</b> 의 <code>xoxb-</code> 값을 복사
                </>,
                <>
                  아래 등록 폼의 <b>봇 토큰</b> 칸에 붙여넣기
                </>,
              ]}
            />
            <Shot src="/guide/03-bot-token.png" alt="OAuth & Permissions 화면의 Bot User OAuth Token 위치" />
            <p className="step-note">
              워크스페이스 관리자 승인이 필요한 조직이면 이 단계에서 승인 요청이 올라갑니다.
              승인이 날 때까지 토큰이 보이지 않습니다.
            </p>
          </Step>

          <Step n={4} title="이 화면에서 워크스페이스 등록" where="지금 보고 있는 화면">
            <Todo
              items={[
                <>
                  <b>키</b> 입력 — 영문 소문자·숫자·하이픈 (예: <code>fin</code>,{' '}
                  <code>site-gimhae</code>)
                </>,
                <>
                  <b>표시 이름</b> 입력 (예: 자금팀)
                </>,
                <>2단계에서 받은 앱 토큰, 3단계에서 받은 봇 토큰 붙여넣기</>,
                <>
                  <b>하루 사용 상한</b> 확인 (기본 $2)
                </>,
                <>
                  <b>워크스페이스 등록</b> 클릭
                </>,
              ]}
            />
            <div className="rule-list">
              <div className="rule bad">
                <span className="rule-mark">주의</span>
                <span>
                  <b>키는 나중에 바꿀 수 없습니다.</b> 대화를 저장하는 폴더 이름과 문서 안의 표시에
                  함께 쓰이기 때문입니다.
                </span>
              </div>
              <div className="rule bad">
                <span className="rule-mark">주의</span>
                <span>
                  <b>상위 워크스페이스</b> 체크는 산하 조직 자료를 모두 열람하는 권한입니다. 취합이
                  업무인 조직에만 지정해 주세요.
                </span>
              </div>
            </div>
          </Step>

          <Step n={5} title="채널에 봇 초대" where="Slack 앱">
            <Todo
              items={[
                <>대화를 모을 채널 열기</>,
                <>
                  입력창에 <code>/invite @tybot</code> 입력
                </>,
                <>모을 채널마다 반복</>,
              ]}
            />
            <div className="rule-list">
              <div className="rule bad">
                <span className="rule-mark">중요</span>
                <span>
                  <b>초대하지 않은 채널은 수집도 조회도 되지 않습니다.</b>
                </span>
              </div>
              <div className="rule ok">
                <span className="rule-mark">참고</span>
                <span>
                  공개 채널은 봇에게 <code>@tybot 전체수집</code> 이라고 하면 스스로 들어갑니다.
                  <b> 비공개 채널은 반드시 사람이 초대해야 합니다.</b>
                </span>
              </div>
            </div>
          </Step>

          <Step n={6} title="동작 확인" where="Slack 앱 · 데이터 현황">
            <Todo
              items={[
                <>
                  채널에서 <code>@tybot 상태</code> 입력 → 연결 상태와 모은 문서 수가 오면 정상
                </>,
                <>
                  <code>@tybot 수집</code> 입력 → 그 채널의 지난 대화를 채워 넣습니다
                </>,
                <>
                  콘솔의 <b>데이터 현황</b> 화면에서 대화가 쌓이는지 확인
                </>,
              ]}
            />
            <p className="step-note">
              며칠 뒤에도 그래프가 비어 있으면 초대가 빠졌거나 서버 저장 경로에 문제가 있는
              경우입니다. 그때는 <b>데이터 현황</b> 화면에 사유가 함께 표시됩니다.
            </p>
          </Step>
        </ol>

        <div className="callout">
          <span>
            봇은 <b>초대된 채널의 대화만</b> 모읍니다. 등기부등본·계약자 명단·주민번호처럼 개인정보가
            담긴 자료는 자동으로 걸러 저장하지 않습니다. 다만 걸러지지 않는 형태로 올라올 수 있으니,
            민감한 자료는 봇이 들어간 채널에 올리지 않는 편이 안전합니다.
          </span>
        </div>
      </div>
    </details>
  )
}
