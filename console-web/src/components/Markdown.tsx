/** 마크다운 미리보기 — 외부 라이브러리 없이 직접 렌더링합니다.
 *
 * 왜 직접 만드나:
 * 이 화면에 들어오는 텍스트는 **사내 Slack 대화 원문**과 **현업이 편집한 규칙 파일**입니다.
 * 둘 다 사람이 쓴 임의의 문자열이고, 그대로 HTML 로 넣으면 스크립트가 실행될 수 있습니다.
 * 그래서 순서를 고정합니다 — **먼저 전부 이스케이프하고, 그다음 우리가 아는 문법만 태그로 바꿉니다.**
 * 원문에 `<script>` 가 적혀 있으면 화면에는 글자 `<script>` 로 보입니다.
 *
 * 지원 문법: 제목(#), 목록(-, 1.), 인용(>), 코드블록(```), 인라인코드(`),
 *            굵게(**), 구분선(---), 표(|), 프론트매터(--- ... ---)
 * 링크는 태그로 바꾸지 않고 글자 그대로 둡니다. 콘솔에서 외부로 나가는 클릭 경로를 만들지 않습니다.
 */

const ESCAPES: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}

function esc(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ESCAPES[c])
}

/** 이스케이프가 끝난 문자열에만 적용합니다 */
function inline(s: string): string {
  return s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
}

/** 아카이브 원문 라인: `> [2026-08-12 09:15] 홍길동: 내용` */
const RAW_LINE = /^\[([^\]]+)\]\s*([^:]+):\s*(.*)$/

function quoteLine(body: string): string {
  const m = RAW_LINE.exec(body)
  if (!m) return `<blockquote>${inline(esc(body))}</blockquote>`
  return (
    `<blockquote><span class="ts">${esc(m[1])}</span>` +
    `<span class="who">${esc(m[2].trim())}</span> ${inline(esc(m[3]))}</blockquote>`
  )
}

function tableRow(line: string, cell: 'th' | 'td'): string {
  const cells = line
    .replace(/^\||\|$/g, '')
    .split('|')
    .map((c) => `<${cell}>${inline(esc(c.trim()))}</${cell}>`)
    .join('')
  return `<tr>${cells}</tr>`
}

function isDivider(line: string): boolean {
  return /^\|?[\s:|-]+\|?$/.test(line) && line.includes('-') && line.includes('|')
}

export function renderMarkdown(src: string): string {
  const lines = src.replace(/\r\n/g, '\n').split('\n')
  const out: string[] = []
  let i = 0
  let list: 'ul' | 'ol' | null = null

  const closeList = () => {
    if (list) {
      out.push(`</${list}>`)
      list = null
    }
  }

  // 파일 맨 앞 프론트매터는 표로 따로 보여주므로 본문에서는 건너뜁니다
  if (lines[0]?.trim() === '---') {
    const end = lines.indexOf('---', 1)
    if (end > 0) i = end + 1
  }

  for (; i < lines.length; i++) {
    const line = lines[i]
    const t = line.trim()

    if (!t) {
      closeList()
      continue
    }

    // 코드블록
    if (t.startsWith('```')) {
      closeList()
      const buf: string[] = []
      i++
      while (i < lines.length && !lines[i].trim().startsWith('```')) {
        buf.push(lines[i])
        i++
      }
      out.push(`<pre><code>${esc(buf.join('\n'))}</code></pre>`)
      continue
    }

    // 표
    if (t.startsWith('|') && isDivider(lines[i + 1]?.trim() ?? '')) {
      closeList()
      const head = tableRow(t, 'th')
      i += 2
      const body: string[] = []
      while (i < lines.length && lines[i].trim().startsWith('|')) {
        body.push(tableRow(lines[i].trim(), 'td'))
        i++
      }
      i--
      out.push(`<table><thead>${head}</thead><tbody>${body.join('')}</tbody></table>`)
      continue
    }

    if (/^---+$/.test(t)) {
      closeList()
      out.push('<hr />')
      continue
    }

    const h = /^(#{1,3})\s+(.*)$/.exec(t)
    if (h) {
      closeList()
      const level = h[1].length
      out.push(`<h${level}>${inline(esc(h[2]))}</h${level}>`)
      continue
    }

    if (t.startsWith('>')) {
      closeList()
      out.push(quoteLine(t.replace(/^>\s?/, '')))
      continue
    }

    const ul = /^[-*]\s+(.*)$/.exec(t)
    if (ul) {
      if (list !== 'ul') {
        closeList()
        out.push('<ul>')
        list = 'ul'
      }
      out.push(`<li>${inline(esc(ul[1]))}</li>`)
      continue
    }

    const ol = /^\d+\.\s+(.*)$/.exec(t)
    if (ol) {
      if (list !== 'ol') {
        closeList()
        out.push('<ol>')
        list = 'ol'
      }
      out.push(`<li>${inline(esc(ol[1]))}</li>`)
      continue
    }

    closeList()
    out.push(`<p>${inline(esc(t))}</p>`)
  }
  closeList()
  return out.join('\n')
}

/** 파일 맨 앞 프론트매터를 키·값 목록으로 뽑습니다. 본문 미리보기와 따로 보여줍니다. */
export function parseFrontmatter(src: string): { key: string; value: string }[] {
  const lines = src.replace(/\r\n/g, '\n').split('\n')
  if (lines[0]?.trim() !== '---') return []
  const end = lines.indexOf('---', 1)
  if (end < 0) return []
  const out: { key: string; value: string }[] = []
  for (const raw of lines.slice(1, end)) {
    const idx = raw.indexOf(':')
    if (idx < 1) continue
    out.push({ key: raw.slice(0, idx).trim(), value: raw.slice(idx + 1).trim() || '(비어 있음)' })
  }
  return out
}

export function Markdown({ source }: { source: string }) {
  return <div className="md" dangerouslySetInnerHTML={{ __html: renderMarkdown(source) }} />
}

export function Frontmatter({ source }: { source: string }) {
  const rows = parseFrontmatter(source)
  if (!rows.length) return null
  return (
    <div className="fm">
      {rows.map((r) => (
        <div className="fm-row" key={r.key}>
          <div className="fm-k">{r.key}</div>
          <div className="fm-v">{r.value}</div>
        </div>
      ))}
    </div>
  )
}
