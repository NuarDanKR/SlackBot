/**
 * 검토 후보 프롬프트. **`contract/summary-review.md` 를 그대로 읽는다.**
 *
 * ## 왜 베껴 적지 않나
 *
 * 그 파일은 PF Hermes 와 TYBot 이 **같이 보는 계약**이다. 여기 한 벌을 더 두면
 * 한쪽만 고쳐지는 날이 오고, 그때 「어느 규칙이 실제로 걸렸나」 를 판정할 기준이
 * 없다. 규칙이 느슨해진 것도 에러 없이 흐른다 — 후보가 좀 더 많아질 뿐이다.
 *
 * ## 딱 한 줄을 덧붙인다
 *
 * 계약 파일은 `source_id` 를 모른다. 그건 **이 RPC 의 전선 규약**이지 후보 규칙이
 * 아니다. 그래서 계약 본문은 한 글자도 안 건드리고, 뒤에 그 한 가지만 붙인다.
 * 붙이는 자리를 여기 한 곳으로 모아 두면 「누가 프롬프트를 늘렸나」 가 보인다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const REVIEW_CONTRACT_FILE = path.resolve(HERE, '..', '..', 'contract', 'summary-review.md');

/** 전선 규약 한 가지. **후보 규칙을 바꾸지 않는다** — 어느 원문에 기댔는지만 더 묻는다. */
const SOURCE_ID_RULE = [
  '',
  '- 후보마다 `source_id` 를 함께 출력합니다. 값은 주어진 원문 앞의 대괄호 ID',
  '  (`r1`, `r2` …) 그대로이고, `evidence_quote` 를 **그 원문에서** 복사했다는 뜻입니다.',
  '  다른 원문의 문장을 인용하면서 다른 ID 를 적은 후보는 버려집니다.',
].join('\n');

/**
 * 검토 프롬프트 전문.
 *
 * **읽기를 실패시키지 않는다.** 계약 파일이 없으면 던진다 — 기본 문구로 물러서면
 * 그 날부터 규칙 없는 후보가 만들어지고, 검토자는 그것을 승인한다.
 */
export function renderReviewPrompt({ read = fs.readFileSync } = {}) {
  let contract;
  try {
    contract = String(read(REVIEW_CONTRACT_FILE, 'utf8'));
  } catch (err) {
    throw new Error(
      `검토 계약 파일을 읽지 못했습니다(${err.code || err.message}): ${REVIEW_CONTRACT_FILE}\n`
      + '  기본 문구로 물러서지 않습니다 — 규칙 없는 후보를 검토자가 승인하게 됩니다.',
    );
  }
  if (!contract.trim()) {
    throw new Error(`검토 계약 파일이 비었습니다: ${REVIEW_CONTRACT_FILE}`);
  }
  return `${contract.trimEnd()}\n${SOURCE_ID_RULE}\n`;
}
