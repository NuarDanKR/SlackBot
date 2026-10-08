/**
 * RPC worker 의 시스템 프롬프트.
 *
 * ## 아카이브 색인을 싣지 않는다
 *
 * 독립 런타임의 Q&A 는 system 블록에 **아카이브 색인**(약 2.7만 토큰)을 싣는다. 여기서는
 * 싣지 않는다 — 그 색인이 로컬 자료이고, RPC 에서 자료는 도구로만 들어오기 때문이다
 * (인계 요구 6). 색인이 없으면 모델은 처음부터 `search` 로 간다. 그게 맞는 모양이다.
 *
 * 덤으로 캐시 셈이 단순해진다. 요청마다 권한이 다른데 색인은 권한별로 갈리므로,
 * 싣는 순간 캐시는 요청마다 깨지고 비용만 는다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { terms as domainTerms } from '../domain.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const PROMPT_FILE = path.resolve(HERE, '..', 'prompts', 'rpc-answer.md');

/** 치환 자리. `llm/prompts.js` 와 **같은 이름**을 쓴다 — 두 벌의 규칙을 만들지 않는다. */
const KEY_RE = /\{\{(EX_[A-Z]|PLACE|AREA|MAX_CHARS)\}\}/g;

/**
 * 프롬프트를 읽어 치환한다.
 *
 * **남은 `{{...}}` 가 있으면 던진다.** 그대로 내보내면 모델에게 중괄호가 그냥 글자로
 * 보이고, 그건 오류가 아니라 **조금 이상한 답**으로만 나타난다 — 아무도 신고하지 않는다.
 */
export function renderRpcPrompt({ domain, maxOutputChars, read = fs.readFileSync }) {
  const t = domainTerms(domain);
  const values = {
    ...t.examples,
    PLACE: t.place,
    AREA: t.area,
    MAX_CHARS: String(maxOutputChars),
  };
  const out = String(read(PROMPT_FILE, 'utf8')).replace(KEY_RE, (whole, key) => (
    key in values ? values[key] : whole
  ));
  const left = out.match(/\{\{[^}]+\}\}/g);
  if (left) {
    throw new Error(`프롬프트에 치환되지 않은 자리가 남았습니다: ${[...new Set(left)].join(', ')}`);
  }
  return out;
}
