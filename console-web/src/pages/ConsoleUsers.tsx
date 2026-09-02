import { useEffect, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { Failed, Loading, PageHead, Section } from '../components/primitives'
import type { ConsoleRole, ConsoleUser } from '../types'

interface AccountRow {
  email: string
  name: string
  role: ConsoleRole
  active: boolean
  workspaces: string[]
  created_at: string
  last_seen: string | null
}

interface AccountList {
  users: AccountRow[]
  roles: ConsoleRole[]
}

interface Draft {
  email: string
  name: string
  role: ConsoleRole
  active: boolean
  workspaces: string
  password: string
}

const EMPTY: Draft = {
  email: '',
  name: '',
  role: 'guest',
  active: true,
  workspaces: '',
  password: '',
}

const ROLE_LABEL: Record<ConsoleRole, string> = {
  guest: '게스트',
  developer: '개발자',
  admin: '관리자',
}

export function ConsoleUsers({
  currentUser,
  onToast,
}: {
  currentUser: ConsoleUser
  onToast: (message: string) => void
}) {
  const resource = useResource<AccountList>('/api/console-users')
  const [draft, setDraft] = useState<Draft>(EMPTY)
  const [editingEmail, setEditingEmail] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => setError(null), [draft.email])

  if (resource.loading && !resource.data) return <Loading what="콘솔 사용자를" />
  if (resource.error && !resource.data) {
    return <Failed what="콘솔 사용자를" detail={resource.error.message} onRetry={resource.reload} />
  }

  const rows = resource.data?.users ?? []

  function edit(row: AccountRow) {
    setEditingEmail(row.email)
    setDraft({
      email: row.email,
      name: row.name,
      role: row.role,
      active: row.active,
      workspaces: row.workspaces.join(', '),
      password: '',
    })
  }

  async function save(e: React.FormEvent) {
    e.preventDefault()
    setSaving(true)
    setError(null)
    try {
      await api.put('/api/console-users', {
        email: draft.email.trim(),
        name: draft.name.trim(),
        role: draft.role,
        active: draft.active,
        workspaces: draft.workspaces
          .split(',')
          .map((value) => value.trim())
          .filter(Boolean),
        password: draft.password || null,
      })
      onToast('콘솔 사용자 설정을 저장했습니다.')
      setDraft(EMPTY)
      setEditingEmail(null)
      resource.reload()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause))
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <PageHead
        crumb="관리 · 접근 권한"
        title="콘솔 사용자 관리"
        note="회사 이메일을 로그인 ID로 사용하며 역할과 담당 워크스페이스를 관리합니다."
      />

      <Section title="등록 사용자" note={`${rows.length}명`}>
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>이메일</th>
                <th>이름</th>
                <th>권한</th>
                <th>범위</th>
                <th>상태</th>
                <th className="right">관리</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.email}>
                  <td className="mono">{row.email}</td>
                  <td>{row.name}</td>
                  <td>{ROLE_LABEL[row.role]}</td>
                  <td>{row.role === 'admin' ? '전체' : row.workspaces.join(', ') || '없음'}</td>
                  <td>{row.active ? '사용 중' : '비활성'}</td>
                  <td className="right">
                    <button className="btn btn-sm" type="button" onClick={() => edit(row)}>
                      수정
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <Section title={editingEmail ? '사용자 수정' : '사용자 추가'}>
        <form onSubmit={save}>
          <div className="form-grid">
            <label className="field">
              <span className="field-label">회사 이메일</span>
              <input
                className="input"
                type="email"
                required
                disabled={editingEmail !== null}
                value={draft.email}
                onChange={(e) => setDraft({ ...draft, email: e.target.value })}
              />
            </label>
            <label className="field">
              <span className="field-label">표시 이름</span>
              <input
                className="input"
                required
                value={draft.name}
                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
              />
            </label>
            <label className="field">
              <span className="field-label">권한</span>
              <select
                className="input"
                value={draft.role}
                disabled={draft.email === currentUser.email}
                onChange={(e) => setDraft({ ...draft, role: e.target.value as ConsoleRole })}
              >
                <option value="guest">게스트</option>
                <option value="developer">개발자</option>
                <option value="admin">관리자</option>
              </select>
            </label>
            <label className="field">
              <span className="field-label">담당 워크스페이스</span>
              <input
                className="input"
                disabled={draft.role === 'admin'}
                placeholder="예: pilot, TYIT"
                value={draft.workspaces}
                onChange={(e) => setDraft({ ...draft, workspaces: e.target.value })}
              />
            </label>
            <label className="field">
              <span className="field-label">새 비밀번호</span>
              <input
                className="input"
                type="password"
                minLength={8}
                placeholder="기존 사용자는 비워 두면 유지"
                value={draft.password}
                onChange={(e) => setDraft({ ...draft, password: e.target.value })}
              />
            </label>
            <label className="check-line compact">
              <input
                type="checkbox"
                checked={draft.active}
                disabled={draft.email === currentUser.email}
                onChange={(e) => setDraft({ ...draft, active: e.target.checked })}
              />
              <span>사용 가능</span>
            </label>
          </div>
          {error && <div className="notice bad form-row">{error}</div>}
          <div className="form-row">
            <button className="btn btn-primary" type="submit" disabled={saving}>
              {saving ? '저장 중…' : '저장'}
            </button>
            <button
              className="btn"
              type="button"
              onClick={() => {
                setDraft(EMPTY)
                setEditingEmail(null)
              }}
            >
              새 사용자
            </button>
          </div>
        </form>
      </Section>
    </>
  )
}
