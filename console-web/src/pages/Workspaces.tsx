/** 워크스페이스 관리 — 서버의 설정 파일을 직접 고치는 절차를 대체합니다.
 *
 * 지금은 워크스페이스를 늘릴 때 서버에 접속해 `tybot.env` 를 편집하고 프로세스를 다시 띄웁니다.
 * 그 방식에서는 토큰 한 줄만 잘못 넣어도 **모든 워크스페이스의 봇이 뜨지 않습니다.**
 * 여기서 등록하면 문제가 생긴 워크스페이스만 멈추고 나머지는 계속 동작합니다.
 *
 * 토큰은 입력만 받고 화면에는 가려서 보여 줍니다. 저장한 값을 다시 읽는 기능은 두지 않습니다.
 */
import { useState } from 'react'
import { registry } from '../mock/data'
import { SetupGuide } from '../components/SetupGuide'
import { MockBadge, PageHead, Section, fmt } from '../components/primitives'

const KEY_RE = /^[a-z][a-z0-9-]{1,23}$/

export function Workspaces({ onToast }: { onToast: (m: string) => void }) {
  const [key, setKey] = useState('')
  const [label, setLabel] = useState('')
  const [bot, setBot] = useState('')
  const [app, setApp] = useState('')
  const [limit, setLimit] = useState('2')
  const [root, setRoot] = useState(false)

  const taken = registry.some((r) => r.key === key.trim())
  const keyOk = KEY_RE.test(key.trim()) && !taken
  const ready =
    keyOk && label.trim().length > 0 && bot.startsWith('xoxb-') && app.startsWith('xapp-')

  const keyProblem = !key.trim()
    ? null
    : taken
      ? '이미 사용 중인 키입니다. 수집을 시작한 뒤에는 키를 바꿀 수 없으니 다른 이름을 써 주세요.'
      : !KEY_RE.test(key.trim())
        ? '영문 소문자로 시작하고, 소문자·숫자·하이픈만 쓸 수 있습니다. 길이는 2~24자입니다.'
        : null

  return (
    <>
      <PageHead
        crumb="봇 관리 · 워크스페이스 관리"
        title="워크스페이스 관리"
        note="새 워크스페이스에 봇을 붙이거나 토큰을 교체합니다. 서버에 접속해 설정 파일을 고칠 필요가 없습니다. 등록에 문제가 있으면 해당 워크스페이스만 멈추고 다른 봇은 그대로 동작합니다."
        aside={
          <>
            <MockBadge />
            <span className="chip flat">등록 {registry.length}개</span>
          </>
        }
      />

      <div className="section">
        <SetupGuide />
      </div>

      <Section
        title="새 워크스페이스 등록"
        note="등록 후 채널 초대까지 해야 수집이 시작됩니다"
        lead="Slack 에서 앱을 만들어 받은 두 개의 토큰과, 이 워크스페이스를 가리킬 짧은 키를 입력합니다. 처음이시면 위의 안내를 먼저 펼쳐 보세요."
      >
        <div className="card card-pad">
          <div className="form-grid">
            <div className="field">
              <label className="field-label" htmlFor="ws-key">
                키 (짧은 이름)
              </label>
              <input
                id="ws-key"
                className="input mono"
                placeholder="site-gimhae"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                aria-describedby="ws-key-help"
              />
              <span className={keyProblem ? 'hint warn' : 'field-help'} id="ws-key-help">
                {keyProblem ??
                  '저장 폴더 이름과 권한 판정에 함께 쓰입니다. 조직을 알 수 있게 짧게 정해 주세요. 수집을 시작한 뒤에는 바꿀 수 없습니다.'}
              </span>
            </div>

            <div className="field">
              <label className="field-label" htmlFor="ws-label">
                표시 이름
              </label>
              <input
                id="ws-label"
                className="input"
                placeholder="현장 김해외동(180182)"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
              />
              <span className="field-help">
                사람이 읽는 이름입니다. 봇 상태 안내와 답변 출처에 이 이름이 나옵니다.
              </span>
            </div>

            <div className="field">
              <label className="field-label" htmlFor="ws-bot">
                봇 토큰
              </label>
              <input
                id="ws-bot"
                className="input mono"
                type="password"
                placeholder="xoxb-"
                value={bot}
                onChange={(e) => setBot(e.target.value)}
                autoComplete="off"
              />
              <span className="field-help">
                Slack 앱을 워크스페이스에 설치하면 받는 값입니다. 저장 후에는 다시 볼 수 없습니다.
              </span>
            </div>

            <div className="field">
              <label className="field-label" htmlFor="ws-app">
                앱 토큰
              </label>
              <input
                id="ws-app"
                className="input mono"
                type="password"
                placeholder="xapp-"
                value={app}
                onChange={(e) => setApp(e.target.value)}
                autoComplete="off"
              />
              <span className="field-help">
                Slack 과 연결을 유지하는 데 쓰입니다. 앱 설정에서 connections:write 권한으로
                발급합니다.
              </span>
            </div>

            <div className="field">
              <label className="field-label" htmlFor="ws-limit">
                하루 사용 상한 (달러)
              </label>
              <input
                id="ws-limit"
                className="input mono"
                inputMode="decimal"
                value={limit}
                onChange={(e) => setLimit(e.target.value)}
              />
              <span className="field-help">
                이 워크스페이스만의 상한입니다. 넘으면 이 봇의 호출만 멈추고 다른 봇은 계속
                답합니다.
              </span>
            </div>

            <div className="field">
              <span className="field-label">등급</span>
              <label className="check-line">
                <input
                  type="checkbox"
                  checked={root}
                  onChange={(e) => setRoot(e.target.checked)}
                  style={{ marginTop: 4 }}
                />
                <span className="field-help">
                  <b>상위 워크스페이스로 지정</b> — 산하 워크스페이스의 자료를 공유 표시와 상관없이
                  모두 볼 수 있고, 자기 워크스페이스 안에서는 채널 구성원 여부와 무관하게 조회할 수
                  있습니다. 경영본부처럼 취합·열람이 업무인 조직에만 지정해 주세요.
                </span>
              </label>
            </div>
          </div>

          <div className="callout" style={{ marginTop: 18 }}>
            <span>
              토큰은 암호화해 저장하고 <b>가려진 형태로만</b> 화면에 표시합니다. 저장한 값을 다시
              꺼내 보는 기능은 서버에도 없습니다. 잘못 입력했다면 새 값으로 덮어써 주세요.
            </span>
          </div>

          <div className="form-row">
            <button
              className="btn btn-primary"
              disabled={!ready}
              onClick={() => {
                onToast(
                  `${label.trim()} 워크스페이스를 등록했습니다. 채널에서 /invite @tybot 을 입력하면 수집이 시작됩니다.`,
                )
                setKey('')
                setLabel('')
                setBot('')
                setApp('')
              }}
            >
              워크스페이스 등록
            </button>
            <span className="field-help">
              등록해도 채널에 봇을 초대하기 전까지는 아무것도 수집되지 않습니다.
            </span>
          </div>
        </div>
      </Section>

      <Section
        title="등록된 워크스페이스"
        note="문제가 생긴 곳만 멈춥니다"
        lead="토큰 갱신 시각과 동작 상태를 확인합니다. 오류가 있는 워크스페이스는 그 봇만 멈추고 나머지는 계속 동작합니다."
      >
        <div className="card card-pad">
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th>키 · 이름</th>
                  <th>등급</th>
                  <th>봇 토큰</th>
                  <th>앱 토큰</th>
                  <th>토큰 갱신</th>
                  <th className="num">상한</th>
                  <th>상태</th>
                  <th className="right">관리</th>
                </tr>
              </thead>
              <tbody>
                {registry.map((r) => (
                  <tr key={r.key}>
                    <td>
                      <div className="mono">{r.key}</div>
                      <div style={{ color: 'var(--text-2)', fontSize: 12.5 }}>{r.label}</div>
                    </td>
                    <td>{r.role === 'root' ? '상위' : '일반'}</td>
                    <td className="mono">{r.botTokenMask}</td>
                    <td className="mono">{r.appTokenMask}</td>
                    <td>
                      <div className="mono">{fmt.dayClock(r.secretUpdatedAt)}</div>
                      <div style={{ color: 'var(--text-3)', fontSize: 12 }}>{r.secretUpdatedBy}</div>
                    </td>
                    <td className="num">{fmt.usd(r.limitUsd)}</td>
                    <td>
                      {r.state === 'enabled' ? (
                        <span className="chip ok">동작 중</span>
                      ) : r.state === 'error' ? (
                        <span className="chip bad">오류</span>
                      ) : (
                        <span className="chip flat">중지</span>
                      )}
                      {r.error && (
                        <p className="hint warn" style={{ marginTop: 5, maxWidth: 270 }}>
                          {r.error}
                        </p>
                      )}
                    </td>
                    <td className="right">
                      <button
                        className="btn btn-sm btn-quiet"
                        onClick={() => onToast(`${r.label} 의 토큰 교체 창을 열었습니다.`)}
                      >
                        토큰 교체
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </Section>

      <Section
        title="워크스페이스 간 열람 설정"
        note="기본값은 서로 볼 수 없음입니다"
        lead="여기서 아무것도 켜지 않으면 워크스페이스끼리 자료가 넘어가지 않습니다."
      >
        <div className="card card-pad">
          <p className="field-help" style={{ maxWidth: '76ch', marginBottom: 14 }}>
            열람 허용은 <b>넘어갈 수 있는 후보</b>만 정합니다. 상위 워크스페이스는 대상 자료를 모두
            볼 수 있고, 같은 등급끼리는 자료를 가진 쪽에서 <code>공유</code> 로 표시한 문서만
            넘어갑니다.
          </p>
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th>보는 쪽</th>
                  <th>열람 대상</th>
                  <th>넘어가는 범위</th>
                </tr>
              </thead>
              <tbody>
                {registry.map((r) => (
                  <tr key={r.key}>
                    <td className="mono">{r.key}</td>
                    <td className="mono">
                      {r.readable.length
                        ? r.readable.join(' · ')
                        : '없음 (자기 워크스페이스만)'}
                    </td>
                    <td style={{ color: 'var(--text-2)', fontSize: 12.5 }}>
                      {r.readable.length === 0
                        ? '—'
                        : r.role === 'root'
                          ? '대상 워크스페이스의 자료 전체'
                          : '공유로 표시된 문서만'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </Section>
    </>
  )
}
