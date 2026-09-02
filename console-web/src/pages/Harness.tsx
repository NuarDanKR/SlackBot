import { useState } from 'react'
import { useResource } from '../api/hooks'
import type { HarnessFile } from '../types'
import { Markdown } from '../components/Markdown'
import { Empty, Failed, Loading, PageHead, Section, fmt } from '../components/primitives'

const KIND_LABEL: Record<HarnessFile['kind'], string> = {
  rules: '답변 규칙',
  workflow: '업무 흐름',
  glossary: '용어 사전',
  prompt: '프롬프트',
}

/** 실제 서버의 규칙 파일만 보여 준다. 쓰기·승인 API가 생기기 전에는 편집 UI를 노출하지 않는다. */
export function Harness() {
  const res = useResource<{ files: HarnessFile[] }>('/api/harness')
  const files = res.data?.files ?? []
  const [selected, setSelected] = useState('')
  const file = files.find((item) => item.path === selected) ?? files[0]

  const byWorkspace = files.reduce<Record<string, HarnessFile[]>>((acc, item) => {
    ;(acc[item.workspaceLabel] ??= []).push(item)
    return acc
  }, {})

  return (
    <>
      <PageHead
        crumb="봇 관리 · 봇 규칙 열람"
        title="봇 규칙 열람"
        note="현재 서버가 실제로 사용하는 답변 규칙과 업무 흐름을 확인합니다. 편집·승인 API가 준비되기 전까지 이 화면에서는 내용을 변경하지 않습니다."
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
          lead="왼쪽에서 워크스페이스와 파일을 고르면 서버에 배포된 현재 내용을 표시합니다."
        >
          <div className="browser">
            <div className="browser-side">
              {Object.entries(byWorkspace).map(([label, list]) => (
                <div key={label}>
                  <div className="tree-group">{label}</div>
                  {list.map((item) => (
                    <button
                      key={item.path}
                      className={`tree-item ${item.path === file.path ? 'is-active' : ''}`}
                      type="button"
                      onClick={() => setSelected(item.path)}
                    >
                      <div className="tree-name">{item.title}</div>
                      <div className="tree-meta">
                        {KIND_LABEL[item.kind]} · {fmt.day(item.updatedAt)} {item.updatedBy}
                      </div>
                    </button>
                  ))}
                </div>
              ))}
            </div>

            <div className="browser-main">
              <div className="browser-bar">
                <div>
                  <div className="card-title">{file.title}</div>
                  <div className="browser-path">{file.path}</div>
                </div>
                <span className="chip flat">읽기 전용</span>
              </div>
              <div className="pane-body">
                <Markdown source={file.content} />
              </div>
            </div>
          </div>
        </Section>
      ) : (
        !res.loading &&
        !res.error && (
          <Empty
            title="등록된 규칙 문서가 없습니다"
            note="서버의 HARNESS_DIR에 워크스페이스별 규칙 파일이 배포되면 여기에 표시됩니다."
          />
        )
      )}
    </>
  )
}
